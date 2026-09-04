"""Private, crash-safe file helpers used by the watcher."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - the watcher is intended for Unix-like hosts
    fcntl = None


class AlreadyRunning(RuntimeError):
    """Raised when another watcher instance already owns the local lock."""


def prepare_private_directory(path: Path) -> None:
    """Create a directory that only the current OS user can access."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def ensure_directory(path: Path) -> None:
    """Create a parent directory without changing an existing project's mode."""
    path.mkdir(parents=True, exist_ok=True)


def prepare_output_directory(path: Path, mode: int) -> None:
    """Create an output directory without chmodding an existing mount."""
    exists = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not exists:
        path.chmod(mode)


def restrict_file(path: Path, mode: int = 0o600) -> None:
    """Restrict an existing private file if it is present."""
    if path.exists():
        path.chmod(mode)


def _temporary_file(destination: Path) -> tuple[int, Path]:
    # Runtime files intentionally live beside the source files.
    # Restrict their file modes, but do not unexpectedly chmod the whole
    # project directory just because an atomic replacement is being written.
    ensure_directory(destination.parent)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    os.fchmod(file_descriptor, 0o600)
    return file_descriptor, temporary_path


def atomic_write_text(destination: Path, text: str, mode: int = 0o600) -> None:
    """Atomically replace a private UTF-8 text file."""
    file_descriptor, temporary_path = _temporary_file(destination)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        destination.chmod(mode)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def atomic_binary_writer(destination: Path, mode: int = 0o600) -> Iterator[BinaryIO]:
    """Yield a private temporary file and atomically publish it on success."""
    file_descriptor, temporary_path = _temporary_file(destination)
    handle: BinaryIO | None = None
    try:
        handle = os.fdopen(file_descriptor, "wb")
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temporary_path, destination)
        destination.chmod(mode)
    except Exception:
        if handle is not None:
            handle.close()
        temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def exclusive_lock(lock_file: Path) -> Iterator[None]:
    """Prevent overlapping scheduled runs on Unix-like hosts."""
    ensure_directory(lock_file.parent)
    with lock_file.open("a", encoding="utf-8") as handle:
        restrict_file(lock_file)
        if fcntl is None:
            # The documented deployment targets are macOS and Linux, both of
            # which support flock. Do not pretend a Windows lock is reliable.
            raise RuntimeError("This watcher requires a Unix-like system lock (fcntl).")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlreadyRunning(
                "Another document watcher run is still in progress."
            ) from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
