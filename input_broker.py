"""External input requests used by source plugins."""

from __future__ import annotations

import queue
import sys
import threading
import time
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


class PushInputBroker:
    """Accept externally pushed answers keyed by account name."""

    def __init__(self, early_answer_ttl_seconds: int = 300) -> None:
        self.early_answer_ttl_seconds = early_answer_ttl_seconds
        self._condition = threading.Condition()
        self._answers: dict[str, tuple[float, dict[str, str]]] = {}
        self._waiting: dict[str, InputChallenge] = {}

    @staticmethod
    def _key(account_name: str) -> str:
        return account_name.strip().lower()

    @staticmethod
    def _field_list(fields: tuple[str, ...] | dict[str, str]) -> str:
        return ", ".join(fields) if fields else "<none>"

    def _discard_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [
            key for key, (expires_at, _) in self._answers.items() if expires_at <= now
        ]
        for key in expired:
            del self._answers[key]

    @staticmethod
    def _answer_for_challenge(
        challenge: InputChallenge, answer: dict[str, str]
    ) -> dict[str, str]:
        missing = [field for field in challenge.fields if not answer.get(field)]
        if missing:
            raise InputUnavailableError(
                f"Input answer for {challenge.account_name} is missing field(s): "
                f"{', '.join(missing)}."
            )
        return {field: answer[field] for field in challenge.fields}

    def provide(
        self,
        account_name: str,
        answer: dict[str, str],
        ttl_seconds: int | None = None,
    ) -> None:
        """Provide an answer for a current or near-future account challenge."""
        key = self._key(account_name)
        if not key:
            raise InputUnavailableError("Input answer is missing an account name.")
        normalized = {
            str(field).strip(): "" if value is None else str(value).strip()
            for field, value in answer.items()
            if str(field).strip()
        }
        if not normalized:
            raise InputUnavailableError("Input answer does not contain any fields.")
        ttl = ttl_seconds if ttl_seconds is not None else self.early_answer_ttl_seconds
        if ttl <= 0:
            raise InputTimeoutError("Input answer TTL must be positive.")
        with self._condition:
            self._discard_expired_locked()
            self._answers[key] = (time.monotonic() + ttl, normalized)
            self._condition.notify_all()
        print(
            f"[{account_name}] Input answer received for field(s): "
            f"{self._field_list(normalized)}; valid for up to {ttl}s."
        )

    def request_input(self, challenge: InputChallenge) -> dict[str, str]:
        """Wait for an externally pushed answer for this account."""
        if challenge.timeout_seconds <= 0:
            raise InputTimeoutError(
                f"Input challenge {challenge.id} for {challenge.account_name} has no time left."
            )
        key = self._key(challenge.account_name)
        if not key:
            raise InputUnavailableError("Input challenge is missing an account name.")
        deadline = time.monotonic() + challenge.timeout_seconds
        with self._condition:
            self._discard_expired_locked()
            if key in self._waiting:
                raise InputUnavailableError(
                    f"Input challenge for {challenge.account_name} is already pending."
                )
            self._waiting[key] = challenge
            self._condition.notify_all()
            print(
                f"[{challenge.account_name}] Input requested by {challenge.source} "
                f"({challenge.kind}); waiting up to {challenge.timeout_seconds}s "
                f"for field(s): {self._field_list(challenge.fields)}."
            )
            try:
                while True:
                    answer = self._answers.pop(key, None)
                    if answer is not None:
                        _, fields = answer
                        result = self._answer_for_challenge(challenge, fields)
                        print(
                            f"[{challenge.account_name}] Input challenge "
                            f"{challenge.id} answered."
                        )
                        return result

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise InputTimeoutError(
                            f"Input challenge {challenge.id} for {challenge.account_name} timed out "
                            f"after {challenge.timeout_seconds} seconds."
                        )
                    self._condition.wait(remaining)
                    self._discard_expired_locked()
            finally:
                self._waiting.pop(key, None)
