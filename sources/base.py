"""Lightweight document-source contract used by the watcher core."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol

if TYPE_CHECKING:
    from outputs.base import OutputConfig


class SourceError(RuntimeError):
    """Base class for expected document-source failures."""


class SourceSessionExpired(SourceError):
    """The source needs its authentication/session to be refreshed."""


class SourceResponseError(SourceError):
    """A source returned data that cannot be safely processed."""


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
        self, account: SourceAccountConfig, seen: set[str]
    ) -> list[SourceMessage]:
        """Return unseen messages in oldest-first processing order."""

    def list_documents(
        self, account: SourceAccountConfig, message: SourceMessage
    ) -> list[SourceDocument]:
        """Return documents belonging to one message."""

    def open_document(
        self,
        account: SourceAccountConfig,
        message: SourceMessage,
        document: SourceDocument,
    ) -> DownloadResponse:
        """Return a streaming document response; the caller closes it."""

    def refresh_authentication(self, account: SourceAccountConfig) -> None:
        """Refresh the account's external authentication/session."""

    def close(self) -> None:
        """Close any source-specific resources after a polling pass."""
