"""Generic polling, checkpointing, and download handling for document sources."""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import tempfile
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from requests import RequestException

from outputs import create_output
from outputs.base import DocumentOutput, LocalDocument, OutputConfig, OutputError
from sources import create_source
from sources.base import (
    DocumentSource,
    DownloadResponse,
    SourceAccountConfig,
    SourceDocument,
    SourceError,
    SourceMessage,
    SourceResponseError,
    SourceSessionExpired,
)
from storage import (
    atomic_binary_writer,
    atomic_write_text,
    exclusive_lock,
    prepare_private_directory,
    restrict_file,
)


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


def load_state(account: SourceAccountConfig) -> dict[str, Any]:
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


def save_state(account: SourceAccountConfig, state: dict[str, Any]) -> None:
    """Atomically checkpoint progress after each completely handled message."""
    atomic_write_text(
        account.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n"
    )


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
    """Collect values for matching keys anywhere in source metadata."""
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
    """Return the first useful short string found in source metadata."""
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
    """Return a stable filename timestamp from source metadata if available."""
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
    message_id: str, metadata: object, detail: object
) -> str:
    """Build a descriptive, bounded prefix from message metadata."""
    components = [
        metadata_date(metadata, detail),
        metadata_text(detail, metadata, field_names=SENDER_FIELD_NAMES),
        metadata_text(detail, metadata, field_names=TITLE_FIELD_NAMES),
        message_id,
    ]
    return "_".join(safe_filename(component, 60) for component in components if component)


def document_filename(message: SourceMessage, document: SourceDocument) -> str:
    """Build a stable, collision-resistant document filename."""
    filename = safe_filename(document.name)
    content_type = document.content_type
    extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ""
    if extension and not filename.lower().endswith(extension.lower()):
        filename += extension
    prefix = attachment_name_prefix(message.id, message.metadata, document.metadata)
    return f"{prefix}_{safe_filename(document.id, 32)}_{filename}"


def document_path(
    account: SourceAccountConfig, message: SourceMessage, document: SourceDocument
) -> Path:
    """Compatibility helper for the default folder destination."""
    for output in account.output_configs:
        if output.type == "folder" and "directory" in output.settings:
            return Path(output.settings["directory"]) / document_filename(
                message, document
            )
    return Path("downloads") / account.name / document_filename(message, document)


def attachment_path(
    account: SourceAccountConfig,
    communication_id: str,
    document_id: str,
    original_name: object,
    content_type: str,
    metadata: dict[str, Any],
    detail: dict[str, Any],
) -> Path:
    """Compatibility wrapper for older MyGuichet helper callers."""
    return document_path(
        account,
        SourceMessage(id=communication_id, metadata=metadata),
        SourceDocument(
            id=document_id,
            name=str(original_name),
            content_type=content_type,
            metadata=detail,
        ),
    )


def write_document(
    response: DownloadResponse,
    destination: Path,
    maximum_bytes: int,
    file_mode: int = 0o600,
) -> None:
    """Stream one document into an atomic private file, enforcing a size cap."""
    try:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > maximum_bytes:
                    raise SourceResponseError(
                        f"Document is larger than the configured {maximum_bytes // (1024 * 1024)} MiB limit."
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
                        raise SourceResponseError(
                            f"Document is larger than the configured {maximum_bytes // (1024 * 1024)} MiB limit."
                        )
                    output.write(chunk)
            except RequestException as error:
                raise SourceResponseError(
                    "Connection failed while downloading a document."
                ) from error
            if total_bytes == 0:
                raise SourceResponseError("Source returned an empty document.")
    finally:
        response.close()


def _temporary_document_file(directory: Path) -> tuple[BinaryIO, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".document.", suffix=".tmp", dir=directory
    )
    temporary_path = Path(temporary_name)
    temporary_path.chmod(0o600)
    return open(file_descriptor, "wb"), temporary_path


def stage_document(
    response: DownloadResponse, directory: Path, maximum_bytes: int
) -> tuple[Path, int]:
    """Stream one source document into a private temporary file."""
    handle: BinaryIO | None = None
    temporary_path: Path | None = None
    try:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > maximum_bytes:
                    raise SourceResponseError(
                        f"Document is larger than the configured {maximum_bytes // (1024 * 1024)} MiB limit."
                    )
            except ValueError:
                pass

        handle, temporary_path = _temporary_document_file(directory)
        total_bytes = 0
        try:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > maximum_bytes:
                    raise SourceResponseError(
                        f"Document is larger than the configured {maximum_bytes // (1024 * 1024)} MiB limit."
                    )
                handle.write(chunk)
        except RequestException as error:
            raise SourceResponseError(
                "Connection failed while downloading a document."
            ) from error
        handle.flush()
        handle.close()
        handle = None
        if total_bytes == 0:
            raise SourceResponseError("Source returned an empty document.")
        return temporary_path, total_bytes
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if handle is not None:
            handle.close()
        response.close()


def default_output_configs(account: SourceAccountConfig) -> tuple[OutputConfig, ...]:
    """Return the legacy folder output when no explicit outputs are configured."""
    return (
        OutputConfig(
            name="folder",
            type="folder",
            settings={
                "directory": Path("downloads") / account.name,
                "file_mode": 0o600,
                "dir_mode": 0o700,
            },
        ),
    )


def configured_outputs(
    account: SourceAccountConfig,
) -> list[tuple[OutputConfig, DocumentOutput]]:
    """Create output adapters for one polling pass."""
    output_configs = account.output_configs or default_output_configs(account)
    return [(config, create_output(config.type)) for config in output_configs]


def close_outputs(outputs: list[tuple[OutputConfig, DocumentOutput]]) -> None:
    """Close output adapters, preserving the first close failure."""
    first_error: Exception | None = None
    for _, output in outputs:
        try:
            output.close()
        except Exception as error:  # pragma: no cover - defensive cleanup
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def deliver_document(
    account: SourceAccountConfig,
    outputs: list[tuple[OutputConfig, DocumentOutput]],
    message: SourceMessage,
    document: SourceDocument,
    local_document: LocalDocument,
) -> None:
    """Deliver one staged document to all configured outputs."""
    if not outputs:
        raise OutputError(f"Account {account.name!r} has no configured outputs.")
    for config, output in outputs:
        output.deliver(account, config, message, document, local_document)


def download_documents(
    account: SourceAccountConfig,
    source: DocumentSource,
    message: SourceMessage,
    outputs: list[tuple[OutputConfig, DocumentOutput]],
) -> int:
    """Fetch all documents for one message and deliver them to every output."""
    documents = source.list_documents(account, message)
    maximum_bytes = account.maximum_document_mb * 1024 * 1024
    downloaded = 0
    for document in documents:
        temporary_path: Path | None = None
        response = source.open_document(account, message, document)
        content_type = response.headers.get("Content-Type", document.content_type)
        if content_type and content_type != document.content_type:
            document = SourceDocument(
                id=document.id,
                name=document.name,
                content_type=content_type,
                metadata=document.metadata,
            )
        temporary_path, size_bytes = stage_document(
            response, account.runtime_dir, maximum_bytes
        )
        try:
            deliver_document(
                account,
                outputs,
                message,
                document,
                LocalDocument(
                    path=temporary_path,
                    filename=document_filename(message, document),
                    content_type=document.content_type,
                    size_bytes=size_bytes,
                ),
            )
            downloaded += 1
        finally:
            temporary_path.unlink(missing_ok=True)
    return downloaded


def run_poll(account: SourceAccountConfig, source: DocumentSource) -> int:
    """Run one polling pass against a source account."""
    prepare_private_directory(account.runtime_dir)
    state = load_state(account)
    seen = set(state["seen_ids"])
    messages = source.collect_unseen(account, seen)
    if not messages:
        state["last_run"] = utc_now()
        save_state(account, state)
        print(f"[{account.name}] No new messages.")
        return 0

    processed = 0
    failed = False
    outputs = configured_outputs(account)
    try:
        for message in messages:
            try:
                document_count = download_documents(account, source, message, outputs)
            except SourceSessionExpired:
                raise
            except (SourceError, OutputError, OSError) as error:
                print(
                    f"[{account.name}] Message {message.id} failed: {error}",
                    file=sys.stderr,
                )
                failed = True
                continue

            seen.add(message.id)
            state["seen_ids"] = sorted(seen)
            state["last_run"] = utc_now()
            save_state(account, state)
            processed += 1
            print(
                f"[{account.name}] Processed message {message.id} "
                f"({document_count} document(s))."
            )
    finally:
        close_outputs(outputs)

    if failed:
        raise SourceError("One or more messages failed; successful messages were checkpointed.")
    return processed


def poll_account(account: SourceAccountConfig) -> int:
    """Poll one account with locking and one expired-session refresh."""
    with exclusive_lock(account.lock_file):
        source = create_source(account.source)
        try:
            try:
                return run_poll(account, source)
            except SourceSessionExpired:
                print(
                    f"[{account.name}] Saved session expired; refreshing "
                    f"{account.source} authentication and retrying once."
                )
                source.refresh_authentication(account)
                source.close()
                source = create_source(account.source)
                return run_poll(account, source)
        finally:
            source.close()
