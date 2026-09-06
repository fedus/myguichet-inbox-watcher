"""Run the watcher when an MQTT message asks for a poll."""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
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
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30
STATUS_SCHEMA = "document_watcher.status.v1"
SHUTDOWN_ERROR_TYPE = "Shutdown"


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


class WorkerState:
    """Track account polls currently running in worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_accounts: set[str] = set()

    def start(self, account: SourceAccountConfig) -> None:
        with self._lock:
            self._active_accounts.add(account.name)

    def finish(self, account: SourceAccountConfig) -> None:
        with self._lock:
            self._active_accounts.discard(account.name)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_accounts)

    def active_accounts(self) -> list[str]:
        with self._lock:
            return sorted(self._active_accounts)


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


def shutdown_timeout_seconds() -> int:
    return _positive_int_env(
        "DOCUMENT_SHUTDOWN_TIMEOUT_SECONDS", DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    )


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


def shutdown_fields(message: str) -> dict[str, str]:
    return {
        "error_type": SHUTDOWN_ERROR_TYPE,
        "error_message": message,
    }


def publish_status(client: object, event: Mapping[str, object]) -> None:
    topic = os.environ.get("DOCUMENT_MQTT_STATUS_TOPIC", "").strip()
    if topic:
        payload = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
        client.publish(topic, payload)  # type: ignore[attr-defined]


def enqueue_poll_request(
    client: object,
    jobs: "queue.Queue[SourceAccountConfig | None]",
    request: PollRequest,
    config_provider: ConfigProvider,
    shutdown_event: threading.Event,
) -> None:
    if shutdown_event.is_set():
        message = "Runner is shutting down; MQTT trigger was ignored."
        print(message, file=sys.stderr)
        publish_status(
            client,
            status_event("trigger.rejected", "error", **shutdown_fields(message)),
        )
        return

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
        if shutdown_event.is_set():
            message = "Runner is shutting down before account could be queued."
            print(f"[{account.name}] {message}", file=sys.stderr)
            publish_status(
                client,
                status_event(
                    "poll.finished",
                    "skipped",
                    account=account.name,
                    source=account.source,
                    **shutdown_fields(message),
                ),
            )
            continue
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
    jobs: "queue.Queue[SourceAccountConfig | None]",
    input_broker: PushInputBroker,
    shutdown_event: threading.Event,
    worker_state: WorkerState,
) -> None:
    while True:
        account = jobs.get()
        try:
            if account is None:
                return

            if shutdown_event.is_set():
                message = "Runner is shutting down before poll started."
                print(f"[{account.name}] {message}")
                publish_status(
                    client,
                    status_event(
                        "poll.finished",
                        "skipped",
                        account=account.name,
                        source=account.source,
                        **shutdown_fields(message),
                    ),
                )
                continue

            worker_state.start(account)
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
            if account is not None:
                worker_state.finish(account)
            jobs.task_done()


def request_shutdown(
    client: object,
    input_broker: PushInputBroker,
    shutdown_event: threading.Event,
    worker_state: WorkerState,
    signal_name: str,
) -> None:
    """Start runner shutdown and unblock MQTT/input waits."""
    if shutdown_event.is_set():
        return
    shutdown_event.set()
    input_broker.close("Runner is shutting down.")
    active_accounts = worker_state.active_accounts()
    print(
        f"Shutdown requested by {signal_name}; waiting for "
        f"{len(active_accounts)} active poll(s) to finish."
    )
    publish_status(
        client,
        status_event(
            "runner.stopping",
            "stopping",
            signal=signal_name,
            active_polls=len(active_accounts),
            active_accounts=active_accounts,
        ),
    )


def wait_for_workers(
    client: object,
    jobs: "queue.Queue[SourceAccountConfig | None]",
    workers: list[threading.Thread],
    worker_state: WorkerState,
    timeout_seconds: int,
) -> None:
    """Wait briefly for workers and report whether any polls remain active."""
    for _ in workers:
        jobs.put(None)

    deadline = time.monotonic() + timeout_seconds
    for worker_thread in workers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        worker_thread.join(timeout=remaining)

    active_accounts = worker_state.active_accounts()
    timed_out = any(worker_thread.is_alive() for worker_thread in workers)
    status = "timeout" if timed_out else "stopped"
    if timed_out:
        print(
            "Shutdown timeout expired; exiting with active poll(s): "
            f"{', '.join(active_accounts) or '<unknown>'}.",
            file=sys.stderr,
        )
    else:
        print("MQTT runner stopped.")
    publish_status(
        client,
        status_event(
            "runner.stopped",
            status,
            active_polls=len(active_accounts),
            active_accounts=active_accounts,
            timed_out=timed_out,
        ),
    )


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

        jobs: "queue.Queue[SourceAccountConfig | None]" = queue.Queue()
        input_broker = PushInputBroker(mqtt_input_ttl_seconds())
        shutdown_event = threading.Event()
        worker_state = WorkerState()
        workers: list[threading.Thread] = []
        worker_count = mqtt_worker_count()
        timeout_seconds = shutdown_timeout_seconds()
        print(
            f"Starting MQTT runner with {worker_count} worker(s); pushed input "
            f"TTL is {input_broker.early_answer_ttl_seconds}s; shutdown timeout "
            f"is {timeout_seconds}s."
        )
        for _ in range(worker_count):
            worker_thread = threading.Thread(
                target=worker,
                args=(client, jobs, input_broker, shutdown_event, worker_state),
                daemon=True,
            )
            workers.append(worker_thread)
            worker_thread.start()

        def handle_signal(signum: int, frame: object) -> None:
            del frame
            signal_name = signal.Signals(signum).name
            request_shutdown(
                client, input_broker, shutdown_event, worker_state, signal_name
            )

        previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)
        previous_sigint = signal.signal(signal.SIGINT, handle_signal)

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
                if shutdown_event.is_set():
                    error = RuntimeError("Runner is shutting down; MQTT input was ignored.")
                    print(f"Rejected MQTT input: {error}", file=sys.stderr)
                    publish_status(
                        client,
                        status_event(
                            "input.rejected", "error", **error_fields(error)
                        ),
                    )
                    return
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
                enqueue_poll_request(client, jobs, request, provider, shutdown_event)
            except ConfigurationError as error:
                print(f"Rejected MQTT trigger: {error}", file=sys.stderr)

        client.on_connect = on_connect  # type: ignore[attr-defined]
        client.on_message = on_message  # type: ignore[attr-defined]
        try:
            client.connect(host, mqtt_port())  # type: ignore[attr-defined]
            while not shutdown_event.is_set():
                client.loop(timeout=1.0)  # type: ignore[attr-defined]
        except KeyboardInterrupt:
            request_shutdown(
                client, input_broker, shutdown_event, worker_state, "KeyboardInterrupt"
            )
        finally:
            shutdown_event.set()
            wait_for_workers(client, jobs, workers, worker_state, timeout_seconds)
            try:
                client.disconnect()  # type: ignore[attr-defined]
                client.loop(timeout=1.0)  # type: ignore[attr-defined]
            except Exception as error:  # pragma: no cover - defensive shutdown path
                print(
                    f"Could not disconnect MQTT client during shutdown: {error}",
                    file=sys.stderr,
                )
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
    except (ConfigurationError, OSError, RuntimeError) as error:
        print(f"MQTT trigger failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
