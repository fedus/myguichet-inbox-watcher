"""Lightweight document-source contract used by the watcher core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Protocol

from input_broker import InputBroker, InputChallenge, InputError

if TYPE_CHECKING:
    from outputs.base import OutputConfig
    from runtime_state import RuntimeState


def _status_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class SourceError(RuntimeError):
    """Base class for expected document-source failures."""


class SourceSessionExpired(SourceError):
    """The source needs its authentication/session to be refreshed."""


class SourceResponseError(SourceError):
    """A source returned data that cannot be safely processed."""


@dataclass(frozen=True)
class SourceContext:
    """Runtime services available to source plugins during one poll."""

    input_broker: InputBroker
    runtime_state: RuntimeState | None = None
    status_callback: Callable[[Mapping[str, object]], None] | None = None

    def _emit_progress(
        self,
        account: str,
        source: str,
        phase: str,
        message: str,
        status: str,
        progress: Mapping[str, int] | None = None,
    ) -> None:
        if self.runtime_state is not None:
            self.runtime_state.poll_progress(
                account,
                source,
                phase,
                status,
                message,
                progress=dict(progress) if progress is not None else None,
            )
        if self.status_callback is not None:
            self.status_callback(
                {
                    "schema": "document_watcher.status.v1",
                    "event": "poll.progress",
                    "status": status,
                    "timestamp": _status_timestamp(),
                    "account": account,
                    "source": source,
                    "phase": phase,
                    "message": message,
                    **(dict(progress) if progress is not None else {}),
                }
            )

    def request_input(self, challenge: InputChallenge) -> dict[str, str]:
        """Request external input and expose failures as source errors."""
        self._emit_progress(
            challenge.account_name,
            challenge.source,
            "waiting_for_authentication",
            challenge.prompt,
            "waiting",
        )
        try:
            answer = self.input_broker.request_input(challenge)
        except InputError as error:
            raise SourceError(str(error)) from error
        self._emit_progress(
            challenge.account_name,
            challenge.source,
            "authenticating",
            "Authentication input received; continuing sign-in.",
            "running",
        )
        return answer

    def report_progress(
        self,
        account: SourceAccountConfig,
        phase: str,
        message: str,
        *,
        status: str = "running",
        progress: Mapping[str, int] | None = None,
    ) -> None:
        """Expose safe progress details to local and service status frontends."""
        self._emit_progress(
            account.name, account.source, phase, message, status, progress
        )

    def report_event(
        self,
        event: str,
        status: str,
        *,
        account: SourceAccountConfig,
        message: str = "",
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Record an event locally and forward it to service status consumers."""
        event_details = dict(details or {})
        if self.runtime_state is not None:
            self.runtime_state.record_event(
                event,
                status,
                account=account.name,
                source=account.source,
                message=message,
                details=event_details,
            )
        if self.status_callback is not None:
            self.status_callback(
                {
                    "schema": "document_watcher.status.v1",
                    "event": event,
                    "status": status,
                    "timestamp": _status_timestamp(),
                    "account": account.name,
                    "source": account.source,
                    "message": message,
                    **(
                        {"error_message": message}
                        if status == "error" and message
                        else {}
                    ),
                    **event_details,
                }
            )


@dataclass(frozen=True)
class SourceAccountConfig:
    """Runtime settings shared by all document-source plugins."""

    name: str
    source: str
    maximum_document_mb: int
    runtime_dir: Path
    state_file: Path
    lock_file: Path
    source_settings: Mapping[str, Any] = field(default_factory=dict)
    output_configs: tuple[OutputConfig, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SourceMessage:
    """One remote message/container that can expose downloadable documents."""

    id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceDocument:
    """One downloadable remote document."""

    id: str
    name: str
    content_type: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class DownloadResponse(Protocol):
    """The minimal streaming response shape the core needs."""

    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int) -> Iterable[bytes]:
        """Yield the response body in chunks."""

    def close(self) -> None:
        """Release the underlying network resource."""


class DocumentSource(Protocol):
    """Adapter between a service-specific client library and the watcher."""

    name: str

    def collect_unseen(
        self, account: SourceAccountConfig, context: SourceContext, seen: set[str]
    ) -> list[SourceMessage]:
        """Return unseen messages in oldest-first processing order."""

    def list_documents(
        self, account: SourceAccountConfig, context: SourceContext, message: SourceMessage
    ) -> list[SourceDocument]:
        """Return documents belonging to one message."""

    def open_document(
        self,
        account: SourceAccountConfig,
        context: SourceContext,
        message: SourceMessage,
        document: SourceDocument,
    ) -> DownloadResponse:
        """Return a streaming document response; the caller closes it."""

    def refresh_authentication(
        self, account: SourceAccountConfig, context: SourceContext
    ) -> None:
        """Refresh the account's external authentication/session."""

    def close(self) -> None:
        """Close any source-specific resources after a polling pass."""
