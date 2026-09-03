"""Download documents from configured inbox sources.

The filename is kept for compatibility with existing cron/Docker setups. The
implementation now delegates source-specific work to lightweight adapters.
"""

from __future__ import annotations

import argparse
import sys

from client import MyGuichetClient, MyGuichetError, PortalResponseError, SessionExpired
from config import (
    ConfigurationError,
    get_source_account,
    get_source_accounts,
    load_environment,
)
from outputs.base import OutputError
from sources import create_source
from sources.base import (
    SourceAccountConfig,
    SourceError,
    SourceResponseError,
    SourceSessionExpired,
)
from sources.myguichet import REQUESTS_PER_PAGE, collect_unseen_communications
from storage import AlreadyRunning, exclusive_lock, prepare_output_directory
from watcher_core import (
    DOWNLOAD_CHUNK_SIZE,
    StateError,
    attachment_name_prefix,
    attachment_path,
    document_path,
    download_documents,
    load_state,
    metadata_date,
    metadata_text,
    metadata_values,
    normalized_field_name,
    parse_portal_date,
    poll_account as core_poll_account,
    run_poll as core_run_poll,
    safe_filename,
    save_state,
    utc_now,
    write_document,
)


def refresh_session(account: object) -> str:
    """Compatibility wrapper for older MyGuichet-only callers."""
    from login_and_grab_cookie import LoginError, refresh_cookie

    try:
        return refresh_cookie(account)  # type: ignore[arg-type]
    except LoginError as error:
        name = getattr(account, "name", "default")
        raise RuntimeError(
            f"Could not refresh the MyGuichet session for {name}: {error}"
        ) from error


def load_cookie(account: object) -> str:
    """Compatibility wrapper for older MyGuichet-only callers."""
    from storage import restrict_file

    cookie_file = getattr(account, "cookie_file")
    if cookie_file.exists():
        restrict_file(cookie_file)
        cookie = cookie_file.read_text(encoding="utf-8").strip()
        if cookie:
            return cookie
    name = getattr(account, "name", "default")
    print(f"[{name}] No usable session cookie found; starting LuxTrust login.")
    return refresh_session(account)


def make_client(account: object, cookie: str) -> MyGuichetClient:
    """Compatibility wrapper for older MyGuichet-only callers."""
    return MyGuichetClient(
        cookie,
        getattr(account, "space_id"),
        getattr(account, "language"),
    )


def run_poll(account: SourceAccountConfig) -> int:
    """Compatibility wrapper around the generic poll implementation."""
    source = create_source(account.source)
    try:
        return core_run_poll(account, source)
    finally:
        source.close()


def poll_account(account: SourceAccountConfig) -> int:
    """Poll one configured source account."""
    return core_poll_account(account)


def write_attachment(*args: object, **kwargs: object) -> None:
    """Compatibility alias for the generic document writer."""
    try:
        write_document(*args, **kwargs)  # type: ignore[arg-type]
    except SourceResponseError as error:
        raise PortalResponseError(str(error)) from error


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download attachments for new inbox messages."
    )
    parser.add_argument(
        "--account",
        help="Poll one configured account. Defaults to all configured accounts.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    total_messages = 0
    failed = False
    try:
        load_environment()
        accounts = (
            [get_source_account(args.account)] if args.account else get_source_accounts()
        )
        for account in accounts:
            try:
                total_messages += poll_account(account)
            except AlreadyRunning as error:
                print(f"[{account.name}] {error}")
            except (
                ConfigurationError,
                MyGuichetError,
                OutputError,
                SourceError,
                OSError,
                RuntimeError,
                StateError,
            ) as error:
                print(f"[{account.name}] Watcher failed: {error}", file=sys.stderr)
                failed = True
    except AlreadyRunning as error:
        print(str(error))
        return 0
    except ConfigurationError as error:
        print(f"Watcher failed: {error}", file=sys.stderr)
        return 1
    if failed:
        return 1
    return total_messages


__all__ = [
    "DOWNLOAD_CHUNK_SIZE",
    "REQUESTS_PER_PAGE",
    "MyGuichetError",
    "PortalResponseError",
    "SessionExpired",
    "SourceResponseError",
    "SourceSessionExpired",
    "StateError",
    "attachment_name_prefix",
    "attachment_path",
    "collect_unseen_communications",
    "document_path",
    "download_documents",
    "load_cookie",
    "load_state",
    "make_client",
    "main",
    "metadata_date",
    "metadata_text",
    "metadata_values",
    "normalized_field_name",
    "parse_args",
    "parse_portal_date",
    "poll_account",
    "prepare_output_directory",
    "exclusive_lock",
    "refresh_session",
    "run_poll",
    "safe_filename",
    "save_state",
    "utc_now",
    "write_attachment",
    "write_document",
]


if __name__ == "__main__":
    raise SystemExit(main())
