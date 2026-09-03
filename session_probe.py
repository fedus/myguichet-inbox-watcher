"""Field test: log whether the current session cookie is still accepted.

Unlike myguichet_get_new_messages.py, this deliberately does NOT refresh an
expired session. Auto-healing would hide the exact moment the original
cookie died and corrupt the measurement.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from client import MyGuichetClient, MyGuichetError, SessionExpired
from config import (
    AccountConfig,
    ConfigurationError,
    ROOT,
    get_myguichet_account,
    get_source_account,
    get_source_accounts,
    load_environment,
)
from storage import restrict_file

LOG_FILE = ROOT / "session_probe.log"


def log(account: AccountConfig, status: str) -> int:
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"{timestamp} {account.name} {status}"
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether saved MyGuichet session cookies are still accepted."
    )
    parser.add_argument(
        "--account",
        help="Probe one configured account. Defaults to all configured accounts.",
    )
    return parser.parse_args(argv)


def probe_account(account: AccountConfig) -> int:
    if not account.cookie_file.exists():
        return log(account, "NO_COOKIE")
    restrict_file(account.cookie_file)
    cookie = account.cookie_file.read_text(encoding="utf-8").strip()
    if not cookie:
        return log(account, "NO_COOKIE")

    client = MyGuichetClient(cookie, account.space_id, account.language)
    try:
        client.list_communications(page=1, per_page=1)
    except SessionExpired:
        return log(account, "EXPIRED")
    except MyGuichetError as error:
        return log(account, f"ERROR {error}")
    else:
        return log(account, "ALIVE")
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    load_environment()
    try:
        source_accounts = (
            [get_source_account(args.account)] if args.account else get_source_accounts()
        )
        accounts = [get_myguichet_account(account) for account in source_accounts]
    except ConfigurationError as error:
        print(f"Probe failed: {error}", file=sys.stderr)
        return 1

    failed = False
    for account in accounts:
        try:
            probe_account(account)
        except (ConfigurationError, OSError) as error:
            print(f"[{account.name}] Probe failed: {error}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
