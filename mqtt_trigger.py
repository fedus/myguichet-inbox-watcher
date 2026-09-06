"""Run the watcher when an MQTT message asks for a poll."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from config import (
    ConfigProvider,
    ConfigurationError,
    EnvConfigProvider,
)
from input_broker import InputError, PushInputBroker
from outputs.base import OutputError
from sources.base import SourceAccountConfig, SourceError
from storage import AlreadyRunning
from watcher_core import StateError, poll_account


DEFAULT_TOPIC = "documents/poll"
DEFAULT_INPUT_TOPIC = "documents/input/provide"
STATUS_SCHEMA = "document_watcher.status.v1"


@dataclass(frozen=True)
class PollRequest:
    """One MQTT-triggered poll request."""

    account_names: list[str] | None
    payload: str


@dataclass(frozen=True)
class InputProvideRequest:
    """One externally provided answer for an account challenge."""

    account_name: str
    fields: dict[str, str]
    payload: str


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive integer.") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return value


def mqtt_port() -> int:
    return _positive_int_env("DOCUMENT_MQTT_PORT", 1883)


def mqtt_input_ttl_seconds() -> int:
    return _positive_int_env("DOCUMENT_MQTT_INPUT_TTL_SECONDS", 300)


def mqtt_worker_count() -> int:
    return _positive_int_env("DOCUMENT_MQTT_WORKERS", 1)


def parse_trigger_payload(payload: str) -> PollRequest:
    """Accept empty/all payloads, a plain account name, or a small JSON payload."""
    text = payload.strip()
    if not text or text.lower() in {"all", "*"}:
        return PollRequest(account_names=None, payload=payload)

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return PollRequest(account_names=[text], payload=payload)

    if isinstance(decoded, str):
        value = decoded.strip()
        if not value or value.lower() in {"all", "*"}:
            return PollRequest(account_names=None, payload=payload)
        return PollRequest(account_names=[value], payload=payload)

    if isinstance(decoded, dict):
        account = decoded.get("account")
        accounts = decoded.get("accounts")
        if isinstance(account, str):
            return PollRequest(account_names=[account], payload=payload)
        if isinstance(accounts, list) and all(
            isinstance(item, str) for item in accounts
        ):
            return PollRequest(account_names=accounts, payload=payload)

    raise ConfigurationError(
        "MQTT payload must be empty, all, an account name, or JSON with account/accounts."
    )


def parse_input_payload(payload: str) -> InputProvideRequest:
    """Parse a generic MQTT input payload keyed by account name."""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ConfigurationError("MQTT input payload must be JSON.") from error
    if not isinstance(decoded, dict):
        raise ConfigurationError("MQTT input payload must be a JSON object.")

    account = decoded.get("for", decoded.get("account"))
    if not isinstance(account, str) or not account.strip():
        raise ConfigurationError(
            "MQTT input payload must include a non-empty 'for' or 'account' value."
        )

    raw_fields = decoded.get("fields")
    fields: dict[str, str]
    if isinstance(raw_fields, dict):
        fields = {
            str(field).strip(): "" if value is None else str(value).strip()
            for field, value in raw_fields.items()
            if str(field).strip()
        }
    elif "code" in decoded:
        raw_code = decoded["code"]
        fields = {"code": "" if raw_code is None else str(raw_code).strip()}
    else:
        raise ConfigurationError(
            "MQTT input payload must include 'code' or a 'fields' object."
        )
    if not fields or any(not value for value in fields.values()):
        raise ConfigurationError("MQTT input payload contains an empty input value.")

    return InputProvideRequest(account.strip(), fields, payload)


def resolve_accounts(
    request: PollRequest, config_provider: ConfigProvider
) -> list[SourceAccountConfig]:
    if request.account_names is None:
        return config_provider.get_source_accounts()
    return [config_provider.get_source_account(name) for name in request.account_names]


def describe_poll_request(request: PollRequest) -> str:
    if request.account_names is None:
        return "all configured accounts"
    return ", ".join(request.account_names)


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def status_event(event: str, status: str, **fields: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": STATUS_SCHEMA,
        "event": event,
        "status": status,
        "timestamp": utc_timestamp(),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    return payload


def error_fields(error: BaseException) -> dict[str, str]:
    return {
        "error_type": type(error).__name__,
        "error_message": str(error),
    }


def publish_status(client: object, event: Mapping[str, object]) -> None:
    topic = os.environ.get("DOCUMENT_MQTT_STATUS_TOPIC", "").strip()
    if topic:
        payload = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
        client.publish(topic, payload)  # type: ignore[attr-defined]


def enqueue_poll_request(
    client: object,
    jobs: "queue.Queue[SourceAccountConfig]",
    request: PollRequest,
    config_provider: ConfigProvider,
) -> None:
    try:
        accounts = resolve_accounts(request, config_provider)
    except ConfigurationError as error:
        print(f"ERROR {error}", file=sys.stderr)
        publish_status(
            client,
            status_event("trigger.rejected", "error", **error_fields(error)),
        )
        return

    account_names = ", ".join(account.name for account in accounts)
    print(f"MQTT trigger resolved to account(s): {account_names}")
    for account in accounts:
        jobs.put(account)
        print(f"[{account.name}] Poll queued.")
        publish_status(
            client,
            status_event(
                "poll.queued",
                "queued",
                account=account.name,
                source=account.source,
            ),
        )


def worker(
    client: object,
    jobs: "queue.Queue[SourceAccountConfig]",
    input_broker: PushInputBroker,
) -> None:
    while True:
        account = jobs.get()
        try:
            print(f"[{account.name}] Poll started.")
            publish_status(
                client,
                status_event(
                    "poll.started",
                    "running",
                    account=account.name,
                    source=account.source,
                ),
            )
            try:
                count = poll_account(account, input_broker=input_broker)
            except AlreadyRunning as error:
                print(f"[{account.name}] {error}")
                event = status_event(
                    "poll.finished",
                    "skipped",
                    account=account.name,
                    source=account.source,
                    **error_fields(error),
                )
            except (
                ConfigurationError,
                OutputError,
                SourceError,
                OSError,
                RuntimeError,
                StateError,
            ) as error:
                print(f"[{account.name}] Watcher failed: {error}", file=sys.stderr)
                event = status_event(
                    "poll.finished",
                    "error",
                    account=account.name,
                    source=account.source,
                    **error_fields(error),
                )
            else:
                print(f"[{account.name}] Poll finished: {count} new message(s).")
                event = status_event(
                    "poll.finished",
                    "ok",
                    account=account.name,
                    source=account.source,
                    new_messages=count,
                )
            publish_status(client, event)
        finally:
            jobs.task_done()


def make_client() -> object:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as error:
        raise RuntimeError(
            "paho-mqtt is not installed. Run pip install -r requirements.txt."
        ) from error

    client_id = os.environ.get(
        "DOCUMENT_MQTT_CLIENT_ID", "document-inbox-watcher"
    ).strip()
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


def main(config_provider: ConfigProvider | None = None) -> int:
    provider = config_provider or EnvConfigProvider()
    provider.load()
    host = os.environ.get("DOCUMENT_MQTT_HOST", "").strip()
    if not host:
        print("MQTT trigger failed: DOCUMENT_MQTT_HOST is not set.", file=sys.stderr)
        return 1
    topic = os.environ.get("DOCUMENT_MQTT_TOPIC", DEFAULT_TOPIC).strip()
    topic = topic or DEFAULT_TOPIC
    input_topic = os.environ.get(
        "DOCUMENT_MQTT_INPUT_TOPIC", DEFAULT_INPUT_TOPIC
    ).strip()
    input_topic = input_topic or DEFAULT_INPUT_TOPIC
    if input_topic == topic:
        print(
            "MQTT trigger failed: DOCUMENT_MQTT_INPUT_TOPIC must differ from "
            "DOCUMENT_MQTT_TOPIC.",
            file=sys.stderr,
        )
        return 1

    try:
        client = make_client()
        username = os.environ.get("DOCUMENT_MQTT_USERNAME", "")
        password = os.environ.get("DOCUMENT_MQTT_PASSWORD", "")
        if username:
            client.username_pw_set(  # type: ignore[attr-defined]
                username, password or None
            )

        jobs: "queue.Queue[SourceAccountConfig]" = queue.Queue()
        input_broker = PushInputBroker(mqtt_input_ttl_seconds())
        worker_count = mqtt_worker_count()
        print(
            f"Starting MQTT runner with {worker_count} worker(s); pushed input "
            f"TTL is {input_broker.early_answer_ttl_seconds}s."
        )
        for _ in range(worker_count):
            threading.Thread(
                target=worker, args=(client, jobs, input_broker), daemon=True
            ).start()

        def on_connect(
            client: object,
            userdata: object,
            flags: object,
            reason_code: object,
            properties: object = None,
        ) -> None:
            del userdata, flags, properties
            code = getattr(reason_code, "value", reason_code)
            if code == 0:
                print(
                    f"Connected to MQTT broker at {host}; subscribed to "
                    f"{topic} and {input_topic}."
                )
                client.subscribe(topic)  # type: ignore[attr-defined]
                client.subscribe(input_topic)  # type: ignore[attr-defined]
            else:
                print(
                    f"MQTT connection failed with code {reason_code}.",
                    file=sys.stderr,
                )

        def on_message(client: object, userdata: object, message: object) -> None:
            del userdata
            payload = message.payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            message_topic = str(getattr(message, "topic", ""))
            if message_topic == input_topic:
                try:
                    request = parse_input_payload(payload)
                    input_broker.provide(request.account_name, request.fields)
                except (ConfigurationError, InputError) as error:
                    print(f"Rejected MQTT input: {error}", file=sys.stderr)
                    event = status_event(
                        "input.rejected", "error", **error_fields(error)
                    )
                else:
                    field_names = ", ".join(request.fields)
                    print(
                        f"[{request.account_name}] MQTT input accepted for "
                        f"field(s): {field_names}."
                    )
                    event = status_event(
                        "input.accepted",
                        "ok",
                        account=request.account_name,
                        fields=sorted(request.fields),
                    )
                publish_status(client, event)
                return

            try:
                request = parse_trigger_payload(payload)
                print(
                    f"MQTT trigger received on {message_topic}: "
                    f"{describe_poll_request(request)}."
                )
                enqueue_poll_request(client, jobs, request, provider)
            except ConfigurationError as error:
                print(f"Rejected MQTT trigger: {error}", file=sys.stderr)

        client.on_connect = on_connect  # type: ignore[attr-defined]
        client.on_message = on_message  # type: ignore[attr-defined]
        client.connect(host, mqtt_port())  # type: ignore[attr-defined]
        client.loop_forever()  # type: ignore[attr-defined]
    except (ConfigurationError, OSError, RuntimeError) as error:
        print(f"MQTT trigger failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
