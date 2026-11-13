"""Batch staged documents and copy them to an rclone destination."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from outputs.base import (
    LocalDocument,
    OutputConfig,
    OutputError,
    OutputPollResult,
)
from sources.base import SourceAccountConfig, SourceDocument, SourceMessage


DEFAULT_RETRIES = 5
DEFAULT_LOW_LEVEL_RETRIES = 10
DEFAULT_TIMEOUT_SECONDS = 1800


def _setting(config: OutputConfig, name: str, default: str = "") -> str:
    value = config.settings.get(name, default)
    return str(value).strip()


def _positive_int(config: OutputConfig, name: str, default: int) -> int:
    value = _setting(config, name, str(default))
    try:
        result = int(value)
    except ValueError as error:
        raise OutputError(
            f"rclone output setting {name!r} must be a positive integer."
        ) from error
    if result <= 0:
        raise OutputError(f"rclone output setting {name!r} must be a positive integer.")
    return result


class RcloneOutput:
    """Stage a poll's documents and upload them in one rclone invocation."""

    name = "rclone"

    def __init__(self) -> None:
        self._batch_directory: Path | None = None
        self._batch_config: OutputConfig | None = None
        self._file_count = 0

    def begin_poll(self, account: SourceAccountConfig, config: OutputConfig) -> None:
        if self._batch_directory is not None:
            raise OutputError("rclone output was started twice without being closed.")
        if not _setting(config, "remote"):
            raise OutputError(
                f"rclone output {config.name!r} requires a remote destination setting."
            )
        self._batch_directory = Path(
            tempfile.mkdtemp(
                prefix=f".rclone-{config.name}-", dir=account.runtime_dir
            )
        )
        self._batch_config = config
        self._file_count = 0

    def deliver(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        message: SourceMessage,
        document: SourceDocument,
        local_document: LocalDocument,
    ) -> None:
        del account, message, document
        if self._batch_directory is None or self._batch_config is not config:
            raise OutputError("rclone output received a document outside its poll batch.")

        destination = self._batch_directory / local_document.filename
        if destination.exists():
            stem, suffix = destination.stem, destination.suffix
            index = 2
            while destination.exists():
                destination = self._batch_directory / f"{stem}__{index}{suffix}"
                index += 1
        try:
            shutil.copyfile(local_document.path, destination)
        except OSError as error:
            raise OutputError(
                f"rclone output could not stage {destination.name}: {error}"
            ) from error
        self._file_count += 1

    def end_poll(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        result: OutputPollResult,
    ) -> None:
        del account, result
        if self._batch_directory is None or self._batch_config is not config:
            raise OutputError("rclone output has no active poll batch to upload.")
        if not self._file_count:
            return

        executable = _setting(config, "binary", "rclone")
        remote = _setting(config, "remote")
        config_file = _setting(config, "config_file")
        retries = _positive_int(config, "retries", DEFAULT_RETRIES)
        low_level_retries = _positive_int(
            config, "low_level_retries", DEFAULT_LOW_LEVEL_RETRIES
        )
        timeout = _positive_int(config, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        command = [
            executable,
            "copy",
            str(self._batch_directory),
            remote,
            "--retries",
            str(retries),
            "--low-level-retries",
            str(low_level_retries),
            "--log-level",
            _setting(config, "log_level", "INFO").upper(),
        ]
        if config_file:
            command.extend(("--config", config_file))

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as error:
            raise OutputError(
                f"rclone executable {executable!r} was not found. Install rclone "
                "or set the binary setting."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise OutputError(
                f"rclone upload timed out after {timeout} seconds for {config.name!r}."
            ) from error
        except OSError as error:
            raise OutputError(f"Could not run rclone: {error}") from error

        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout).strip()
            if len(details) > 4000:
                details = details[-4000:]
            suffix = f": {details}" if details else ""
            raise OutputError(
                f"rclone upload for output {config.name!r} failed with exit code "
                f"{completed.returncode}{suffix}"
            )

    def close(self) -> None:
        """Remove staged files after the core has saved successful checkpoints."""
        batch_directory = self._batch_directory
        self._batch_directory = None
        self._batch_config = None
        self._file_count = 0
        if batch_directory is not None:
            shutil.rmtree(batch_directory, ignore_errors=True)
