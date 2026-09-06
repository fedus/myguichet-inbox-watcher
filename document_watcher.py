"""Download documents from configured inbox sources."""

from __future__ import annotations

import argparse
import sys

from config import (
    ConfigProvider,
    ConfigurationError,
    EnvConfigProvider,
)
from outputs.base import OutputError
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


def main(
    argv: list[str] | None = None, config_provider: ConfigProvider | None = None
) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    provider = config_provider or EnvConfigProvider()
    total_messages = 0
    failed = False
    try:
        provider.load()
        accounts = (
            [provider.get_source_account(args.account)]
            if args.account
            else provider.get_source_accounts()
        )
        for account in accounts:
            try:
                total_messages += poll_account(account)
            except AlreadyRunning as error:
                print(f"[{account.name}] {error}")
            except (
                ConfigurationError,
                OutputError,
                SourceError,
                OSError,
                RuntimeError,
                StateError,
            ) as error:
                print(f"[{account.name}] Watcher failed: {error}", file=sys.stderr)
                failed = True
    except ConfigurationError as error:
        print(f"Watcher failed: {error}", file=sys.stderr)
        return 1
    if failed:
        return 1
    return total_messages


if __name__ == "__main__":
    raise SystemExit(main())
