"""Run the read-only API alongside the MQTT watcher."""

from __future__ import annotations

import os
import threading

from api_server import create_app
from config import EnvConfigProvider
from runtime_state import runtime_state


DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8000


def api_host() -> str:
    value = os.environ.get("DOCUMENT_API_HOST", DEFAULT_API_HOST).strip()
    return value or DEFAULT_API_HOST


def api_port() -> int:
    raw_value = os.environ.get("DOCUMENT_API_PORT", str(DEFAULT_API_PORT)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError("DOCUMENT_API_PORT must be a positive integer.") from error
    if value <= 0:
        raise RuntimeError("DOCUMENT_API_PORT must be a positive integer.")
    return value


def run_api(provider: EnvConfigProvider) -> None:
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - exercised by real startup
        raise RuntimeError(
            "Uvicorn is not installed. Run pip install -r requirements.txt."
        ) from error

    app = create_app(provider, runtime_state)
    config = uvicorn.Config(app, host=api_host(), port=api_port(), log_level="info")
    server = uvicorn.Server(config)
    server.run()


def main() -> int:
    import mqtt_trigger

    provider = EnvConfigProvider()
    api_thread = threading.Thread(target=run_api, args=(provider,), daemon=True)
    api_thread.start()
    print(f"Read-only API starting on http://{api_host()}:{api_port()}")
    return mqtt_trigger.main(provider, runtime_state)


if __name__ == "__main__":
    raise SystemExit(main())
