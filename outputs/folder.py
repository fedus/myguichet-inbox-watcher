"""Folder output adapter."""

from __future__ import annotations

import shutil
from pathlib import Path

from outputs.base import LocalDocument, OutputConfig, OutputError
from sources.base import SourceAccountConfig, SourceDocument, SourceMessage
from storage import atomic_binary_writer, prepare_output_directory


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
        del account, message, document
        try:
            directory = Path(config.settings["directory"])
            file_mode = int(config.settings.get("file_mode", 0o600))
            dir_mode = int(config.settings.get("dir_mode", 0o700))
        except (KeyError, TypeError, ValueError) as error:
            raise OutputError(
                f"Folder output {config.name!r} is missing a valid directory configuration."
            ) from error

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
