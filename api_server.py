"""Read-only REST API and minimal dashboard for the document watcher."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import ConfigProvider, EnvConfigProvider
from outputs import available_output_names
from runtime_state import RuntimeState, runtime_state as default_runtime_state
from sources import available_source_names
from sources.base import SourceAccountConfig
from watcher_core import load_state

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse
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
]


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
            "error": None,
        }
    try:
        state = load_state(account)
    except Exception as error:
        return {
            "exists": True,
            "seen_count": None,
            "last_run": None,
            "error": str(error),
        }
    return {
        "exists": True,
        "seen_count": len(state.get("seen_ids", [])),
        "last_run": state.get("last_run"),
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
        description="Read-only account, plugin, runtime, and downloaded-document views.",
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
        for account in accounts():
            if account.name == account_name:
                return account_payload(account)
        raise HTTPException(status_code=404, detail="Unknown account.")

    @app.get("/api/status", tags=["runtime"])
    def status() -> dict[str, Any]:
        return state.snapshot()

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
