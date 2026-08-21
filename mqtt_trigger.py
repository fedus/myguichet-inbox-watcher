"""Run the watcher when an MQTT message asks for a poll."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from dataclasses import dataclass

from client import MyGuichetError
from config import (
    AccountConfig,
    ConfigurationError,
    get_account,
    get_accounts,
    load_environment,
)
from myguichet_get_new_messages import StateError, poll_account
from storage import AlreadyRunning


DEFAULT_TOPIC = "myguichet/poll"


@dataclass(frozen=True)
class PollRequest:
    """One MQTT-triggered poll request."""

    account_names: list[str] | None
    payload: str


def mqtt_port() -> int:
    raw_value = os.environ.get("MYGUICHET_MQTT_PORT", "1883").strip()
    try:
        port = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(
            "MYGUICHET_MQTT_PORT must be a positive integer."
        ) from error
    if port <= 0:
        raise ConfigurationError("MYGUICHET_MQTT_PORT must be a positive integer.")
    return port


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


def resolve_accounts(request: PollRequest) -> list[AccountConfig]:
    if request.account_names is None:
        return get_accounts()
    return [get_account(name) for name in request.account_names]


def publish_status(client: object, status: str) -> None:
    topic = os.environ.get("MYGUICHET_MQTT_STATUS_TOPIC", "").strip()
    if topic:
        client.publish(topic, status)  # type: ignore[attr-defined]


def worker(client: object, jobs: "queue.Queue[PollRequest]") -> None:
    while True:
        request = jobs.get()
        try:
            try:
                accounts = resolve_accounts(request)
            except ConfigurationError as error:
                message = f"ERROR {error}"
                print(message, file=sys.stderr)
                publish_status(client, message)
                continue

            for account in accounts:
                try:
                    count = poll_account(account)
                except AlreadyRunning as error:
                    message = f"{account.name} SKIPPED {error}"
                    print(f"[{account.name}] {error}")
                except (
                    ConfigurationError,
                    MyGuichetError,
                    OSError,
                    RuntimeError,
                    StateError,
                ) as error:
                    message = f"{account.name} ERROR {error}"
                    print(f"[{account.name}] Watcher failed: {error}", file=sys.stderr)
                else:
                    message = f"{account.name} OK {count}"
                publish_status(client, message)
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
        "MYGUICHET_MQTT_CLIENT_ID", "myguichet-inbox-watcher"
    ).strip()
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


def main() -> int:
    load_environment()
    host = os.environ.get("MYGUICHET_MQTT_HOST", "").strip()
    if not host:
        print("MQTT trigger failed: MYGUICHET_MQTT_HOST is not set.", file=sys.stderr)
        return 1
    topic = os.environ.get("MYGUICHET_MQTT_TOPIC", DEFAULT_TOPIC).strip()
    topic = topic or DEFAULT_TOPIC

    try:
        client = make_client()
        username = os.environ.get("MYGUICHET_MQTT_USERNAME", "")
        password = os.environ.get("MYGUICHET_MQTT_PASSWORD", "")
        if username:
            client.username_pw_set(  # type: ignore[attr-defined]
                username, password or None
            )

        jobs: "queue.Queue[PollRequest]" = queue.Queue()
        threading.Thread(target=worker, args=(client, jobs), daemon=True).start()

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
                print(f"Connected to MQTT broker at {host}; subscribed to {topic}.")
                client.subscribe(topic)  # type: ignore[attr-defined]
            else:
                print(
                    f"MQTT connection failed with code {reason_code}.",
                    file=sys.stderr,
                )

        def on_message(client: object, userdata: object, message: object) -> None:
            del client, userdata
            payload = message.payload.decode(  # type: ignore[attr-defined]
                "utf-8", errors="replace"
            )
            try:
                jobs.put(parse_trigger_payload(payload))
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
