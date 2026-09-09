"""Download documents from configured inbox sources."""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from config import (
    ConfigProvider,
    ConfigurationError,
    EnvConfigProvider,
)
from outputs.base import OutputError
from runtime_state import runtime_state
from sources.base import SourceError
from storage import AlreadyRunning
from watcher_core import StateError, poll_account


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download documents for new inbox messages."
    )
    parser.add_argument(
        "--account",
        help="Poll one configured account. Defaults to all configured accounts.",
    )
    return parser.parse_args(argv)


def install_shutdown_handlers(
    shutdown_event: threading.Event,
) -> tuple[signal.Handlers, signal.Handlers]:
    """Handle the first stop signal gracefully and let a second one interrupt."""

    def handle_signal(signum: int, frame: object) -> None:
        del frame
        signal_name = signal.Signals(signum).name
        if shutdown_event.is_set():
            raise KeyboardInterrupt
        shutdown_event.set()
        print(
            f"Shutdown requested by {signal_name}; finishing the current account "
            "before exiting.",
            file=sys.stderr,
        )

    previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    previous_sigint = signal.signal(signal.SIGINT, handle_signal)
    return previous_sigterm, previous_sigint


def main(
    argv: list[str] | None = None, config_provider: ConfigProvider | None = None
) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    provider = config_provider or EnvConfigProvider()
    total_messages = 0
    failed = False
    shutdown_event = threading.Event()
    previous_sigterm: signal.Handlers | None = None
    previous_sigint: signal.Handlers | None = None
    try:
        provider.load()
        accounts = (
            [provider.get_source_account(args.account)]
            if args.account
            else provider.get_source_accounts()
        )
        previous_sigterm, previous_sigint = install_shutdown_handlers(shutdown_event)
        for account in accounts:
            if shutdown_event.is_set():
                print(f"[{account.name}] Skipped because shutdown was requested.")
                break
            try:
                runtime_state.poll_started(account.name, account.source)
                count = poll_account(account, runtime_state=runtime_state)
                total_messages += count
                runtime_state.poll_finished(
                    account.name,
                    account.source,
                    "ok",
                    new_messages=count,
                )
            except AlreadyRunning as error:
                print(f"[{account.name}] {error}")
                runtime_state.poll_finished(
                    account.name,
                    account.source,
                    "skipped",
                    error_type=type(error).__name__,
                    error_message=str(error),
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
                runtime_state.poll_finished(
                    account.name,
                    account.source,
                    "error",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                failed = True
            except Exception as error:
                print(f"[{account.name}] Watcher failed: {error}", file=sys.stderr)
                runtime_state.poll_finished(
                    account.name,
                    account.source,
                    "error",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                failed = True
    except ConfigurationError as error:
        print(f"Watcher failed: {error}", file=sys.stderr)
        runtime_state.record_event(
            "watcher.failed",
            "error",
            message=str(error),
            details={"error_type": type(error).__name__},
        )
        return 1
    except KeyboardInterrupt:
        print("Watcher interrupted.", file=sys.stderr)
        return 130
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)
    if failed:
        return 1
    return total_messages


if __name__ == "__main__":
    raise SystemExit(main())
