"""Document-source registry.

The registry is intentionally tiny: a source is just a Python adapter class
that implements the protocol in ``sources.base``. Add new built-in sources by
registering them here, or register from local code before calling the poller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sources.base import DocumentSource


SourceFactory = Callable[[], "DocumentSource"]
_SOURCES: dict[str, SourceFactory] = {}
_BUILT_INS_REGISTERED = False


def register_source(name: str, factory: SourceFactory) -> None:
    """Register one document source factory."""
    key = name.strip().lower()
    if not key:
        raise ValueError("Source name cannot be empty.")
    _SOURCES[key] = factory


def create_source(name: str) -> DocumentSource:
    """Create a source adapter by name."""
    from sources.base import SourceError

    _register_built_ins()
    key = name.strip().lower()
    try:
        return _SOURCES[key]()
    except KeyError as error:
        available = ", ".join(sorted(_SOURCES)) or "none"
        raise SourceError(
            f"Unknown document source {name!r}. Available sources: {available}."
        ) from error


def available_source_names() -> set[str]:
    """Return registered source names."""
    _register_built_ins()
    return set(_SOURCES)


def _register_built_ins() -> None:
    global _BUILT_INS_REGISTERED
    if _BUILT_INS_REGISTERED:
        return
    from sources.myguichet import MyGuichetDocumentSource

    register_source("myguichet", MyGuichetDocumentSource)
    _BUILT_INS_REGISTERED = True
