"""REST API and minimal dashboard for the document watcher."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import ConfigProvider, EnvConfigProvider
from outputs import available_output_names
from runtime_state import RuntimeState, runtime_state as default_runtime_state
from sources import available_source_names
from sources.base import SourceAccountConfig
from storage import AlreadyRunning
from watcher_core import StateError, clear_seen_messages, load_state, unsee_message

try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import FileResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as error:  # pragma: no cover - exercised by real startup
    raise RuntimeError(
        "FastAPI is not installed. Run pip install -r requirements.txt."
    ) from error


SENSITIVE_SETTING_PARTS = (
    "access",
    "credential",
    "key",
    "password",
    "refresh",
    "secret",
    "token",
    "username",
)
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8000
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
DASHBOARD_INDEX = DASHBOARD_DIR / "index.html"
OPENAPI_TAGS = [
    {
        "name": "runtime",
        "description": "Health, active polls, pending inputs, and events.",
    },
    {
        "name": "configuration",
        "description": "Configured accounts and loaded plugin names.",
    },
    {
        "name": "documents",
        "description": "Downloaded files visible through folder outputs.",
    },
    {
        "name": "service",
        "description": "Read-only HTTP and MQTT service configuration.",
    },
]
MqttPublisher = Callable[..., None]


class MqttTriggerUnavailable(RuntimeError):
    """MQTT triggering is not available in the current service mode."""


class MqttTriggerPublishError(RuntimeError):
    """Publishing an MQTT poll trigger failed."""


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name, str(default)).strip()
    try:
        result = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    if result <= 0:
        raise RuntimeError(f"{name} must be positive.")
    return result


def _positive_int_setting(name: str, value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise MqttTriggerUnavailable(f"{name} must be a positive integer.") from error
    if result <= 0:
        raise MqttTriggerUnavailable(f"{name} must be a positive integer.")
    return result


def is_sensitive_setting(name: str) -> bool:
    normalized = name.lower()
    return any(part in normalized for part in SENSITIVE_SETTING_PARTS)


def sanitize_settings(settings: dict[str, Any] | Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(settings).items():
        result[str(key)] = "***" if is_sensitive_setting(str(key)) else value
    return result


def account_user(account: SourceAccountConfig) -> str:
    suffix = f"_{account.source}"
    if account.name.endswith(suffix):
        return account.name[: -len(suffix)]
    return account.name


def state_summary(account: SourceAccountConfig) -> dict[str, Any]:
    if not account.state_file.exists():
        return {
            "exists": False,
            "seen_count": 0,
            "last_run": None,
            "last_poll": None,
            "error": None,
        }
    try:
        state = load_state(account)
    except Exception as error:
        return {
            "exists": True,
            "seen_count": None,
            "last_run": None,
            "last_poll": None,
            "error": str(error),
        }
    return {
        "exists": True,
        "seen_count": len(state.get("seen_ids", [])),
        "last_run": state.get("last_run"),
        "last_poll": state.get("last_poll"),
        "error": None,
    }


def account_payload(account: SourceAccountConfig) -> dict[str, Any]:
    return {
        "name": account.name,
        "user": account_user(account),
        "source": account.source,
        "runtime_dir": str(account.runtime_dir),
        "state_file": str(account.state_file),
        "maximum_document_mb": account.maximum_document_mb,
        "state": state_summary(account),
        "source_settings": sanitize_settings(dict(account.source_settings)),
        "outputs": [
            {
                "name": output.name,
                "type": output.type,
                "settings": sanitize_settings(dict(output.settings)),
            }
            for output in account.output_configs
        ],
    }


def folder_output_directories(account: SourceAccountConfig) -> list[Path]:
    directories: list[Path] = []
    for output in account.output_configs:
        if output.type != "folder":
            continue
        raw_directory = output.settings.get("directory")
        if raw_directory is None or not str(raw_directory).strip():
            path = Path("downloads") / account.name
        else:
            path = Path(str(raw_directory)).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        directories.append(path)
    return directories


def document_payload(path: Path, account: SourceAccountConfig) -> dict[str, Any]:
    stat = path.stat()
    return {
        "account": account.name,
        "source": account.source,
        "filename": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime_from_ns(stat.st_mtime_ns),
        "modified_ns": stat.st_mtime_ns,
    }


def datetime_from_ns(value: int) -> str:
    seconds = value / 1_000_000_000
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def list_downloaded_documents(
    accounts: list[SourceAccountConfig],
    account_name: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    selected = [
        account
        for account in accounts
        if account_name is None or account.name == account_name
    ]
    for account in selected:
        for directory in folder_output_directories(account):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.is_file() and not path.name.startswith("."):
                    documents.append(document_payload(path, account))
    documents.sort(key=lambda item: int(item["modified_ns"]), reverse=True)
    return documents[:limit]


def latest_persisted_poll(accounts: list[SourceAccountConfig]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for account in accounts:
        if not account.state_file.exists():
            continue
        try:
            state = load_state(account)
        except Exception:
            continue
        poll = state.get("last_poll")
        if not isinstance(poll, dict):
            continue
        candidate = dict(poll)
        candidate.setdefault("account", account.name)
        candidate.setdefault("source", account.source)
        seen_ids = {str(item) for item in state.get("seen_ids", [])}
        documents = []
        for document in candidate.get("documents", []):
            if not isinstance(document, dict):
                continue
            item = dict(document)
            item["seen"] = str(item.get("message_id", "")) in seen_ids
            documents.append(item)
        candidate["documents"] = documents
        if latest is None or str(candidate.get("finished_at", "")) > str(
            latest.get("finished_at", "")
        ):
            latest = candidate
    return latest


def merge_status_with_persisted_poll(
    status: dict[str, Any], accounts: list[SourceAccountConfig]
) -> dict[str, Any]:
    persisted = latest_persisted_poll(accounts)
    current = status.get("last_poll")
    if persisted is None:
        return status
    if current is None or str(persisted.get("finished_at", "")) >= str(
        current.get("finished_at", "")
    ):
        status["last_poll"] = persisted
    return status


def _env_value(environ: Mapping[str, str], name: str, default: str = "") -> str:
    return environ.get(name, default).strip()


def service_payload(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = os.environ if environ is None else environ
    run_mode = _env_value(values, "DOCUMENT_RUN_MODE", "poll") or "poll"
    mqtt_host = _env_value(values, "DOCUMENT_MQTT_HOST")
    mqtt_port = _env_value(values, "DOCUMENT_MQTT_PORT", "1883") or "1883"
    api_host = _env_value(values, "DOCUMENT_API_HOST", DEFAULT_API_HOST)
    api_port = _env_value(values, "DOCUMENT_API_PORT", str(DEFAULT_API_PORT))
    return {
        "run_mode": run_mode,
        "http": {
            "serving": True,
            "bind_host": api_host or DEFAULT_API_HOST,
            "port": api_port or str(DEFAULT_API_PORT),
            "dashboard_path": "/",
            "docs_path": "/docs",
            "openapi_path": "/openapi.json",
        },
        "mqtt": {
            "configured": bool(mqtt_host),
            "running": run_mode in {"mqtt", "watcher"},
            "host": mqtt_host or None,
            "port": mqtt_port,
            "trigger_topic": _env_value(values, "DOCUMENT_MQTT_TOPIC", "documents/poll"),
            "input_topic": _env_value(
                values, "DOCUMENT_MQTT_INPUT_TOPIC", "documents/input/provide"
            ),
            "status_topic": _env_value(values, "DOCUMENT_MQTT_STATUS_TOPIC"),
            "workers": _env_value(values, "DOCUMENT_MQTT_WORKERS", "1") or "1",
        },
    }


def mqtt_publish_single(**kwargs: Any) -> None:
    """Small wrapper to keep the MQTT publish path easy to unit-test."""
    try:
        import paho.mqtt.publish as mqtt_publish
    except ImportError as error:  # pragma: no cover - requirements include paho-mqtt
        raise MqttTriggerPublishError(
            "paho-mqtt is not installed. Run pip install -r requirements.txt."
        ) from error
    mqtt_publish.single(**kwargs)


def publish_mqtt_poll_trigger(
    account: SourceAccountConfig,
    state: RuntimeState,
    *,
    environ: Mapping[str, str] | None = None,
    publisher: MqttPublisher | None = None,
) -> dict[str, Any]:
    """Publish the regular MQTT poll trigger for one configured account."""
    values = os.environ if environ is None else environ
    service = service_payload(values)
    mqtt = service["mqtt"]
    if not mqtt["configured"] or not mqtt["running"]:
        message = (
            "MQTT poll triggers require DOCUMENT_RUN_MODE=watcher or mqtt and "
            "DOCUMENT_MQTT_HOST to be set."
        )
        state.record_event(
            "trigger.rejected",
            "error",
            account=account.name,
            source=account.source,
            message=message,
        )
        raise MqttTriggerUnavailable(message)

    topic = str(mqtt["trigger_topic"])
    payload = json.dumps(
        {"account": account.name},
        sort_keys=True,
        separators=(",", ":"),
    )
    username = _env_value(values, "DOCUMENT_MQTT_USERNAME")
    password = _env_value(values, "DOCUMENT_MQTT_PASSWORD")
    auth = {"username": username, "password": password or None} if username else None
    publish = publisher or mqtt_publish_single
    try:
        publish(
            topic=topic,
            payload=payload,
            hostname=str(mqtt["host"]),
            port=_positive_int_setting("DOCUMENT_MQTT_PORT", str(mqtt["port"])),
            auth=auth,
        )
    except MqttTriggerPublishError:
        raise
    except Exception as error:
        message = f"Could not publish MQTT poll trigger: {error}"
        state.record_event(
            "trigger.publish_failed",
            "error",
            account=account.name,
            source=account.source,
            message=message,
            details={"topic": topic, "error_type": type(error).__name__},
        )
        raise MqttTriggerPublishError(message) from error

    state.record_event(
        "trigger.published",
        "queued",
        account=account.name,
        source=account.source,
        message=f"Published MQTT poll trigger to {topic}.",
        details={"topic": topic},
    )
    return {
        "status": "queued",
        "account": account.name,
        "source": account.source,
        "via": "mqtt",
        "topic": topic,
        "payload": {"account": account.name},
    }


def create_app(
    config_provider: ConfigProvider | None = None,
    runtime_state: RuntimeState | None = None,
) -> FastAPI:
    provider = config_provider or EnvConfigProvider()
    state = runtime_state or default_runtime_state
    provider.load()
    app = FastAPI(
        title="Document Watcher API",
        version="0.1.0",
        description=(
            "Account, plugin, runtime, downloaded-document, and MQTT trigger views."
        ),
        openapi_tags=OPENAPI_TAGS,
    )

    @app.middleware("http")
    async def no_store_observability_responses(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        if (
            request.url.path == "/"
            or request.url.path.startswith("/api/")
            or request.url.path.startswith("/assets/")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/assets", StaticFiles(directory=DASHBOARD_DIR), name="assets")

    def accounts() -> list[SourceAccountConfig]:
        try:
            return provider.get_source_accounts()
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    def account_by_name(account_name: str) -> SourceAccountConfig:
        for account in accounts():
            if account.name == account_name:
                return account
        raise HTTPException(status_code=404, detail="Unknown account.")

    @app.get("/", response_class=FileResponse, include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(DASHBOARD_INDEX)

    @app.get("/api/health", tags=["runtime"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/plugins", tags=["configuration"])
    def plugins() -> dict[str, list[str]]:
        return {
            "sources": sorted(available_source_names()),
            "outputs": sorted(available_output_names()),
        }

    @app.get("/api/accounts", tags=["configuration"])
    def configured_accounts() -> list[dict[str, Any]]:
        return [account_payload(account) for account in accounts()]

    @app.get("/api/accounts/{account_name}", tags=["configuration"])
    def configured_account(account_name: str) -> dict[str, Any]:
        return account_payload(account_by_name(account_name))

    @app.post("/api/accounts/{account_name}/poll", tags=["runtime"])
    def trigger_account_poll(account_name: str) -> dict[str, Any]:
        selected_account = account_by_name(account_name)
        try:
            return publish_mqtt_poll_trigger(selected_account, state)
        except MqttTriggerUnavailable as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except MqttTriggerPublishError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.post("/api/accounts/{account_name}/seen/clear", tags=["runtime"])
    def clear_account_seen(account_name: str) -> dict[str, Any]:
        selected_account = account_by_name(account_name)
        try:
            result = clear_seen_messages(selected_account)
        except AlreadyRunning as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except StateError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        state.record_event(
            "seen.cleared",
            "ok",
            account=selected_account.name,
            source=selected_account.source,
            message=f"Cleared {result['removed_messages']} seen message(s).",
            details={"removed_messages": result["removed_messages"]},
        )
        return result

    @app.post("/api/accounts/{account_name}/seen/unsee", tags=["runtime"])
    def unsee_account_message(
        account_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        selected_account = account_by_name(account_name)
        message_id = str(payload.get("message_id", "")).strip()
        try:
            result = unsee_message(selected_account, message_id)
        except AlreadyRunning as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except StateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        state.record_event(
            "seen.unset",
            "ok",
            account=selected_account.name,
            source=selected_account.source,
            message=(
                f"Marked message {result['message_id']} unseen."
                if result["removed"]
                else f"Message {result['message_id']} was not marked seen."
            ),
            details={
                "message_id": result["message_id"],
                "removed": result["removed"],
            },
        )
        return result

    @app.get("/api/status", tags=["runtime"])
    def status() -> dict[str, Any]:
        return merge_status_with_persisted_poll(state.snapshot(), accounts())

    @app.get("/api/events", tags=["runtime"])
    def events(limit: int = Query(default=200, ge=1, le=500)) -> list[dict[str, Any]]:
        return state.recent_events(limit)

    @app.get("/api/events/stream", tags=["runtime"])
    async def event_stream(
        request: Request,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        async def stream() -> Any:
            last_id = after or state.latest_event_id()
            yield ": connected\n\n"
            while not await request.is_disconnected():
                events = await asyncio.to_thread(
                    state.wait_for_events,
                    last_id,
                    timeout_seconds=15.0,
                )
                if not events:
                    yield ": keep-alive\n\n"
                    continue
                for event in events:
                    last_id = int(event["id"])
                    data = json.dumps(event, separators=(",", ":"))
                    yield f"id: {last_id}\nevent: runtime\ndata: {data}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/service", tags=["service"])
    def service() -> dict[str, Any]:
        return service_payload()

    @app.get("/api/documents", tags=["documents"])
    def documents(
        account: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return list_downloaded_documents(accounts(), account, limit)

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the read-only watcher API.")
    parser.add_argument(
        "--host",
        default=os.environ.get("DOCUMENT_API_HOST", DEFAULT_API_HOST),
        help="Bind host. Defaults to DOCUMENT_API_HOST or 0.0.0.0.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_int_env("DOCUMENT_API_PORT", DEFAULT_API_PORT),
        help="Bind port. Defaults to DOCUMENT_API_PORT or 8000.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=_bool_env("DOCUMENT_API_RELOAD", False),
        help="Enable Uvicorn reload for local development.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - exercised by real startup
        raise RuntimeError(
            "Uvicorn is not installed. Run pip install -r requirements.txt."
        ) from error
    uvicorn.run(
        "api_server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
