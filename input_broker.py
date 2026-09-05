"""External input requests used by source plugins."""

from __future__ import annotations

import queue
import sys
import threading
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4


class InputError(RuntimeError):
    """Base class for expected external-input failures."""


class InputTimeoutError(InputError):
    """A requested external input was not provided before its deadline."""


class InputUnavailableError(InputError):
    """No input provider is available for the current run mode."""


@dataclass(frozen=True)
class InputChallenge:
    """One account/source-specific request for human-provided data."""

    account_name: str
    source: str
    kind: str
    prompt: str
    fields: tuple[str, ...] = ("code",)
    timeout_seconds: int = 300
    id: str = field(default_factory=lambda: uuid4().hex)


class InputBroker(Protocol):
    """Provider that can answer source-plugin input challenges."""

    def request_input(self, challenge: InputChallenge) -> dict[str, str]:
        """Return field values for one challenge or raise an input error."""


class CliInputBroker:
    """Read challenge answers from stdin with a hard timeout."""

    def request_input(self, challenge: InputChallenge) -> dict[str, str]:
        if challenge.timeout_seconds <= 0:
            raise InputTimeoutError(
                f"Input challenge {challenge.id} for {challenge.account_name} has no time left."
            )
        if not sys.stdin.isatty():
            raise InputUnavailableError(
                f"Input challenge {challenge.id} for {challenge.account_name} needs "
                "interactive input, but stdin is not a terminal."
            )

        answers: "queue.Queue[dict[str, str] | BaseException]" = queue.Queue(maxsize=1)

        def read_answers() -> None:
            try:
                print(
                    f"[{challenge.account_name}] {challenge.prompt} "
                    f"(challenge {challenge.id}, expires in {challenge.timeout_seconds}s)"
                )
                result: dict[str, str] = {}
                for field_name in challenge.fields:
                    value = input(f"{field_name}: ").strip()
                    if not value:
                        raise InputUnavailableError(
                            f"Input challenge {challenge.id} field {field_name!r} was empty."
                        )
                    result[field_name] = value
                answers.put(result)
            except BaseException as error:  # pragma: no cover - defensive thread bridge
                answers.put(error)

        threading.Thread(target=read_answers, daemon=True).start()
        try:
            result = answers.get(timeout=challenge.timeout_seconds)
        except queue.Empty as error:
            raise InputTimeoutError(
                f"Input challenge {challenge.id} for {challenge.account_name} timed out "
                f"after {challenge.timeout_seconds} seconds."
            ) from error
        if isinstance(result, BaseException):
            raise result
        return result
