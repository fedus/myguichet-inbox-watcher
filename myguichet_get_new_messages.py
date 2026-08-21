"""Download attachments for MyGuichet messages not yet checkpointed locally."""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import argparse
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from requests import RequestException, Response

from client import MyGuichetClient, MyGuichetError, PortalResponseError, SessionExpired
from config import (
    AccountConfig,
    ConfigurationError,
    get_account,
    get_accounts,
    load_environment,
)
from storage import (
    AlreadyRunning,
    atomic_binary_writer,
    atomic_write_text,
    exclusive_lock,
    prepare_output_directory,
    prepare_private_directory,
    restrict_file,
)


REQUESTS_PER_PAGE = 100
DOWNLOAD_CHUNK_SIZE = 64 * 1024
TITLE_FIELD_NAMES = {
    "communicationlabel",
    "communicationsubject",
    "label",
    "libelle",
    "messageobject",
    "messagesubject",
    "objet",
    "object",
    "subject",
    "title",
}
SENDER_FIELD_NAMES = {
    "author",
    "emetteur",
    "emitter",
    "expediteur",
    "expeditor",
    "issuer",
    "sender",
    "senderdisplayname",
    "sendername",
}
NESTED_TEXT_FIELD_NAMES = {
    "displayname",
    "label",
    "libelle",
    "name",
    "title",
}
DATE_FIELD_NAMES = {
    "communicationdate",
    "creationdate",
    "date",
    "depositdate",
    "emissiondate",
    "publisheddate",
    "receiveddate",
    "sentdate",
    "sendingdate",
}


class StateError(RuntimeError):
    """The local progress file cannot be read safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(account: AccountConfig) -> dict[str, Any]:
    """Read and validate the private progress file without silently replacing it."""
    if not account.state_file.exists():
        return {"seen_ids": []}
    restrict_file(account.state_file)
    try:
        state = json.loads(account.state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(
            f"Could not read {account.state_file}; restore it from a backup or remove it intentionally."
        ) from error
    if not isinstance(state, dict) or not isinstance(state.get("seen_ids", []), list):
        raise StateError(f"{account.state_file} has an invalid format.")
    state["seen_ids"] = [str(item) for item in state["seen_ids"]]
    return state


def save_state(account: AccountConfig, state: dict[str, Any]) -> None:
    """Atomically checkpoint progress after each completely handled message."""
    atomic_write_text(
        account.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n"
    )


def refresh_session(account: AccountConfig) -> str:
    """Lazy-load Playwright only when a session is absent or expired."""
    from login_and_grab_cookie import LoginError, refresh_cookie

    try:
        return refresh_cookie(account)
    except LoginError as error:
        raise RuntimeError(
            f"Could not refresh the MyGuichet session for {account.name}: {error}"
        ) from error


def load_cookie(account: AccountConfig) -> str:
    """Return cookie.txt, obtaining an authenticated session when needed."""
    if account.cookie_file.exists():
        restrict_file(account.cookie_file)
        cookie = account.cookie_file.read_text(encoding="utf-8").strip()
        if cookie:
            return cookie
    print(f"[{account.name}] No usable session cookie found; starting LuxTrust login.")
    return refresh_session(account)


def make_client(account: AccountConfig, cookie: str) -> MyGuichetClient:
    return MyGuichetClient(cookie, account.space_id, account.language)


def collect_unseen_communications(
    client: MyGuichetClient, seen: set[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Page through the inbox until reaching an already-seen section.

    The API returns newest messages first. On a normal run this stops after the
    first already-seen page; on a first run it intentionally processes the
    whole available inbox.
    """
    unseen: list[tuple[str, dict[str, Any]]] = []
    collected_ids: set[str] = set()
    page_number = 1
    retrieved_count = 0
    while True:
        payload = client.list_communications(
            page=page_number, per_page=REQUESTS_PER_PAGE
        )
        raw_items = payload.get("myCommunicationList", [])
        if not isinstance(raw_items, list):
            raise PortalResponseError(
                "Portal response does not contain a communication list."
            )
        if not raw_items:
            break
        retrieved_count += len(raw_items)

        page_ids: list[str] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            hit = item.get("eDeliveryCommunicationHitDto") or {}
            if not isinstance(hit, dict) or hit.get("id") is None:
                continue
            communication_id = str(hit["id"])
            page_ids.append(communication_id)
            if communication_id not in seen and communication_id not in collected_ids:
                unseen.append((communication_id, hit))
                collected_ids.add(communication_id)

        total = payload.get("nbTotalCommunication")
        # Use actual received items rather than the requested page size: the
        # server may cap requestsPerPage to a lower value.
        at_end = isinstance(total, int) and retrieved_count >= total
        if at_end or (
            not isinstance(total, int) and len(raw_items) < REQUESTS_PER_PAGE
        ):
            break
        if page_ids and all(communication_id in seen for communication_id in page_ids):
            break
        page_number += 1

    # Download chronologically where possible, rather than newest-first.
    unseen.reverse()
    return unseen


def safe_filename(value: object, maximum_length: int = 120) -> str:
    """Return a portable, bounded filename component with no path traversal."""
    text = str(value)
    text = re.sub(r"[<>:\"/\\\\|?*\x00-\x1f]", "_", text)
    text = text.strip(". ")
    return text[:maximum_length].rstrip(". ") or "document"


def normalized_field_name(value: object) -> str:
    """Return a loose key name for matching portal metadata fields."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def metadata_values(value: object, field_names: set[str]) -> list[object]:
    """Collect values for matching keys anywhere in a portal metadata object."""
    matches: list[object] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if normalized_field_name(key) in field_names:
                matches.append(item)
            matches.extend(metadata_values(item, field_names))
    elif isinstance(value, list):
        for item in value:
            matches.extend(metadata_values(item, field_names))
    return matches


def metadata_text(*sources: object, field_names: set[str]) -> str:
    """Return the first useful short string found in portal metadata."""
    for source in sources:
        for value in metadata_values(source, field_names):
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
            elif isinstance(value, (int, float)):
                return str(value)
            elif isinstance(value, dict):
                text = metadata_text(value, field_names=NESTED_TEXT_FIELD_NAMES)
                if text:
                    return text
    return ""


def parse_portal_date(value: str) -> datetime | None:
    """Parse common portal/API date formats into a datetime."""
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        timestamp = int(text)
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OSError, ValueError):
            return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for date_format in (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def metadata_date(*sources: object) -> str:
    """Return a stable filename timestamp from portal metadata if available."""
    for source in sources:
        for value in metadata_values(source, DATE_FIELD_NAMES):
            if isinstance(value, (int, float)):
                parsed = parse_portal_date(str(int(value)))
            elif isinstance(value, str):
                parsed = parse_portal_date(value)
            else:
                parsed = None
            if parsed is not None:
                return parsed.strftime("%Y-%m-%d_%H%M%S")
    return ""


def attachment_name_prefix(
    communication_id: str, metadata: dict[str, Any], detail: dict[str, Any]
) -> str:
    """Build a descriptive, bounded prefix from message metadata."""
    components = [
        metadata_date(metadata, detail),
        metadata_text(detail, metadata, field_names=SENDER_FIELD_NAMES),
        metadata_text(detail, metadata, field_names=TITLE_FIELD_NAMES),
        communication_id,
    ]
    return "_".join(safe_filename(component, 60) for component in components if component)


def attachment_path(
    account: AccountConfig,
    communication_id: str,
    document_id: str,
    original_name: object,
    content_type: str,
    metadata: dict[str, Any],
    detail: dict[str, Any],
) -> Path:
    """Build a stable, collision-resistant private attachment destination."""
    filename = safe_filename(original_name)
    extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ""
    if extension and not filename.lower().endswith(extension.lower()):
        filename += extension
    prefix = attachment_name_prefix(communication_id, metadata, detail)
    return (
        account.download_dir
        / f"{prefix}_{safe_filename(document_id, 32)}_{filename}"
    )


def write_attachment(
    response: Response, destination: Path, maximum_bytes: int, file_mode: int = 0o600
) -> None:
    """Stream one attachment into an atomic private file, enforcing a size cap."""
    try:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > maximum_bytes:
                    raise PortalResponseError(
                        f"Attachment is larger than the configured {maximum_bytes // (1024 * 1024)} MiB limit."
                    )
            except ValueError:
                pass

        total_bytes = 0
        with atomic_binary_writer(destination, mode=file_mode) as output:
            try:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > maximum_bytes:
                        raise PortalResponseError(
                            f"Attachment is larger than the configured {maximum_bytes // (1024 * 1024)} MiB limit."
                        )
                    output.write(chunk)
            except RequestException as error:
                raise PortalResponseError(
                    "Connection failed while downloading an attachment."
                ) from error
            # Raising inside the atomic writer removes the temporary file
            # instead of publishing an empty final document.
            if total_bytes == 0:
                raise PortalResponseError("Portal returned an empty attachment.")
    finally:
        response.close()


def download_attachments(
    account: AccountConfig,
    client: MyGuichetClient,
    communication_id: str,
    metadata: dict[str, Any],
) -> int:
    """Download all attachments for one message before it is marked as seen."""
    detail = client.get_edelivery(communication_id)
    attachments = detail.get("attachmentList", [])
    if not isinstance(attachments, list):
        raise PortalResponseError(
            f"Message {communication_id} has an invalid attachment list."
        )

    prepare_output_directory(account.download_dir, account.download_dir_mode)
    maximum_bytes = account.maximum_attachment_mb * 1024 * 1024
    downloaded = 0
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        document_id = attachment.get("externalDocId")
        if not document_id:
            continue
        original_name = attachment.get("docName") or str(document_id)
        response = client.download_document(str(document_id), str(original_name))
        destination = attachment_path(
            account,
            communication_id,
            str(document_id),
            original_name,
            response.headers.get("Content-Type", ""),
            metadata,
            detail,
        )
        write_attachment(
            response, destination, maximum_bytes, file_mode=account.download_file_mode
        )
        downloaded += 1
    return downloaded


def run_poll(account: AccountConfig) -> int:
    """Run one polling pass using the current session cookie."""
    prepare_private_directory(account.runtime_dir)
    state = load_state(account)
    seen = set(state["seen_ids"])
    client = make_client(account, load_cookie(account))
    try:
        messages = collect_unseen_communications(client, seen)
        if not messages:
            state["last_run"] = utc_now()
            save_state(account, state)
            print(f"[{account.name}] No new messages.")
            return 0

        for communication_id, metadata in messages:
            attachment_count = download_attachments(
                account, client, communication_id, metadata
            )
            seen.add(communication_id)
            state["seen_ids"] = sorted(seen)
            state["last_run"] = utc_now()
            save_state(account, state)
            print(
                f"[{account.name}] Processed message {communication_id} "
                f"({attachment_count} attachment(s))."
            )
        return len(messages)
    finally:
        client.close()


def poll_account(account: AccountConfig) -> int:
    """Poll one account with locking and one expired-session refresh."""
    with exclusive_lock(account.lock_file):
        try:
            return run_poll(account)
        except SessionExpired:
            print(
                f"[{account.name}] Saved session expired; starting LuxTrust login "
                "and retrying once."
            )
            refresh_session(account)
            return run_poll(account)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download attachments for new MyGuichet inbox messages."
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
        accounts = [get_account(args.account)] if args.account else get_accounts()
        for account in accounts:
            try:
                total_messages += poll_account(account)
            except AlreadyRunning as error:
                print(f"[{account.name}] {error}")
            except (
                ConfigurationError,
                MyGuichetError,
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


if __name__ == "__main__":
    raise SystemExit(main())
