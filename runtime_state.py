"""In-process runtime status shared by watcher frontends."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Condition, RLock
from typing import Any


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class RuntimeEvent:
    """One recent watcher event suitable for logs and dashboards."""

    id: int
    timestamp: str
    event: str
    status: str
    account: str | None = None
    source: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class RuntimeState:
    """Small thread-safe runtime state for read-only API/dashboard views."""

    def __init__(self, max_events: int = 200) -> None:
        self._condition = Condition(RLock())
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._active_polls: dict[str, dict[str, Any]] = {}
        self._pending_inputs: dict[str, dict[str, Any]] = {}
        self._next_event_id = 1

    def record_event(
        self,
        event: str,
        status: str,
        *,
        account: str | None = None,
        source: str | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._condition:
            item = RuntimeEvent(
                id=self._next_event_id,
                timestamp=utc_timestamp(),
                event=event,
                status=status,
                account=account,
                source=source,
                message=message,
                details=details or {},
            )
            self._next_event_id += 1
            self._events.append(item)
            self._condition.notify_all()

    def poll_started(self, account: str, source: str) -> None:
        started_at = utc_timestamp()
        with self._condition:
            self._active_polls[account] = {
                "account": account,
                "source": source,
                "started_at": started_at,
            }
        self.record_event("poll.started", "running", account=account, source=source)

    def poll_finished(
        self,
        account: str,
        source: str,
        status: str,
        *,
        new_messages: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if new_messages is not None:
            details["new_messages"] = new_messages
        if error_type:
            details["error_type"] = error_type
        if error_message:
            details["error_message"] = error_message
        with self._condition:
            self._active_polls.pop(account, None)
        self.record_event(
            "poll.finished",
            status,
            account=account,
            source=source,
            message=error_message or "",
            details=details,
        )

    def input_requested(
        self,
        account: str,
        source: str,
        kind: str,
        fields: tuple[str, ...],
        timeout_seconds: int,
        challenge_id: str,
    ) -> None:
        item = {
            "account": account,
            "source": source,
            "kind": kind,
            "fields": list(fields),
            "timeout_seconds": timeout_seconds,
            "challenge_id": challenge_id,
            "requested_at": utc_timestamp(),
        }
        with self._condition:
            self._pending_inputs[account] = item
        self.record_event(
            "input.requested",
            "waiting",
            account=account,
            source=source,
            details={
                "kind": kind,
                "fields": list(fields),
                "timeout_seconds": timeout_seconds,
                "challenge_id": challenge_id,
            },
        )

    def input_finished(
        self,
        account: str,
        source: str,
        status: str,
        *,
        challenge_id: str,
        message: str = "",
    ) -> None:
        with self._condition:
            self._pending_inputs.pop(account, None)
        self.record_event(
            "input.finished",
            status,
            account=account,
            source=source,
            message=message,
            details={"challenge_id": challenge_id},
        )

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            active_polls = list(self._active_polls.values())
            pending_inputs = list(self._pending_inputs.values())
            events = [event.__dict__ for event in self._events]
        return {
            "active_polls": active_polls,
            "pending_inputs": pending_inputs,
            "recent_events": events,
        }

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._condition:
            return [event.__dict__ for event in list(self._events)[-limit:]]

    def latest_event_id(self) -> int:
        with self._condition:
            if not self._events:
                return 0
            return self._events[-1].id

    def wait_for_events(
        self,
        after_id: int,
        *,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        with self._condition:
            events = [event for event in self._events if event.id > after_id]
            if events:
                return [event.__dict__ for event in events]
            self._condition.wait(timeout_seconds)
            events = [event for event in self._events if event.id > after_id]
            return [event.__dict__ for event in events]


runtime_state = RuntimeState()
