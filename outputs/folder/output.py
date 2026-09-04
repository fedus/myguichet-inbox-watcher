"""Folder output adapter."""

from __future__ import annotations

import shutil
from pathlib import Path

from outputs.base import LocalDocument, OutputConfig, OutputError
from sources.base import SourceAccountConfig, SourceDocument, SourceMessage
from storage import atomic_binary_writer, prepare_output_directory


ROOT = Path(__file__).resolve().parents[2]


def _path_value(raw_value: object, default: Path) -> Path:
    if raw_value is None:
        return default
    value = str(raw_value).strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _octal_mode(raw_value: object, variable: str) -> int:
    if isinstance(raw_value, int):
        result = raw_value
    else:
        try:
            result = int(str(raw_value).strip(), 8)
        except ValueError as error:
            raise OutputError(
                f"Folder output setting {variable} must be an octal Unix permission mode, "
                "such as 0600 or 0644."
            ) from error
    if result < 0 or result > 0o777:
        raise OutputError(
            f"Folder output setting {variable} must be between 0000 and 0777."
        )
    return result


class FolderOutput:
    """Atomically save staged documents into a local folder."""

    name = "folder"

    def deliver(
        self,
        account: SourceAccountConfig,
        config: OutputConfig,
        message: SourceMessage,
        document: SourceDocument,
        local_document: LocalDocument,
    ) -> None:
        del message, document
        settings = config.settings
        directory = _path_value(
            settings.get("directory"), ROOT / "downloads" / account.name
        )
        file_mode = _octal_mode(settings.get("file_mode", 0o600), "FILE_MODE")
        dir_mode = _octal_mode(settings.get("dir_mode", 0o700), "DIR_MODE")

        prepare_output_directory(directory, dir_mode)
        destination = directory / local_document.filename
        try:
            with local_document.path.open("rb") as source:
                with atomic_binary_writer(destination, mode=file_mode) as output:
                    shutil.copyfileobj(source, output)
        except OSError as error:
            raise OutputError(
                f"Folder output {config.name!r} could not write {destination}: {error}"
            ) from error

    def close(self) -> None:
        pass
