"""Document-output registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from outputs.base import DocumentOutput


OutputFactory = Callable[[], "DocumentOutput"]
_OUTPUTS: dict[str, OutputFactory] = {}
_BUILT_INS_REGISTERED = False


def register_output(name: str, factory: OutputFactory) -> None:
    """Register one document output factory."""
    key = name.strip().lower()
    if not key:
        raise ValueError("Output type cannot be empty.")
    _OUTPUTS[key] = factory


def create_output(name: str) -> DocumentOutput:
    """Create an output adapter by type."""
    from outputs.base import OutputError

    _register_built_ins()
    key = name.strip().lower()
    try:
        return _OUTPUTS[key]()
    except KeyError as error:
        available = ", ".join(sorted(_OUTPUTS)) or "none"
        raise OutputError(
            f"Unknown document output {name!r}. Available outputs: {available}."
        ) from error


def available_output_names() -> set[str]:
    """Return registered output adapter names."""
    _register_built_ins()
    return set(_OUTPUTS)


def _register_built_ins() -> None:
    global _BUILT_INS_REGISTERED
    if _BUILT_INS_REGISTERED:
        return
    from outputs.folder import FolderOutput

    register_output("folder", FolderOutput)
    _BUILT_INS_REGISTERED = True
