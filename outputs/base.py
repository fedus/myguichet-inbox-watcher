"""Lightweight document-output contract used by the watcher core."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from sources.base import SourceAccountConfig, SourceDocument, SourceMessage


class OutputError(RuntimeError):
    """Base class for expected document-output failures."""


@dataclass(frozen=True)
class OutputConfig:
    """Per-account configuration for one output adapter."""

    name: str
    type: str
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalDocument:
    """A fetched document staged locally before outputs consume it."""

    path: Path
    filename: str
    content_type: str
    size_bytes: int


class DocumentOutput(Protocol):
    """Adapter that delivers one staged document somewhere."""

    name: str

    def deliver(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        message: SourceMessage,
        document: SourceDocument,
        local_document: LocalDocument,
    ) -> None:
        """Deliver a staged local document for one account/message/document."""

    def close(self) -> None:
        """Close any output-specific resources after a polling pass."""
