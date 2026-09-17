"""Bounded in-memory activity log for the persistent terminal.

Only display metadata is kept: a timestamp, a typed kind, and one safe line.
Raw protocol payloads, prompts, and model output are never stored here.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from code_mule.presentation.terminal import safe_text
from code_mule.progress.contracts import ProgressEvent, ProgressEventType


MAX_ACTIVITY_ENTRIES = 2_000
MAX_ACTIVITY_TEXT = 500


class ActivityKind(StrEnum):
    """Display family for one activity line."""

    INFO = "info"
    COMMAND = "command"
    RESPONSE = "response"
    TASK = "task"
    FAILURE = "failure"
    HUMAN = "human"
    RUNTIME = "runtime"


_KIND_BY_EVENT: dict[ProgressEventType, ActivityKind] = {
    ProgressEventType.WORKER_ACTIVITY: ActivityKind.RESPONSE,
    ProgressEventType.WORKER_STARTING: ActivityKind.TASK,
    ProgressEventType.WORKER_STARTED: ActivityKind.TASK,
    ProgressEventType.WORKER_COMPLETED: ActivityKind.TASK,
    ProgressEventType.WORKER_FAILED: ActivityKind.FAILURE,
    ProgressEventType.WORKER_VERIFICATION_BLOCKED: ActivityKind.FAILURE,
    ProgressEventType.TASK_DISPATCHED: ActivityKind.TASK,
    ProgressEventType.TASK_STARTED: ActivityKind.TASK,
    ProgressEventType.TASK_COMPLETED: ActivityKind.TASK,
    ProgressEventType.TASK_REWORK: ActivityKind.TASK,
    ProgressEventType.TASK_HUMAN_REQUIRED: ActivityKind.HUMAN,
    ProgressEventType.HUMAN_GATE: ActivityKind.HUMAN,
    ProgressEventType.SUPERVISOR_FAILED: ActivityKind.FAILURE,
    ProgressEventType.GIT_DELIVERY_FAILED: ActivityKind.FAILURE,
    ProgressEventType.PLANNING_FAILED: ActivityKind.FAILURE,
    ProgressEventType.REPLANNING_FAILED: ActivityKind.FAILURE,
    ProgressEventType.ERROR: ActivityKind.FAILURE,
    ProgressEventType.PROJECT_VERIFICATION_STARTED: ActivityKind.RUNTIME,
    ProgressEventType.PROJECT_VERIFICATION_COMPLETED: ActivityKind.RUNTIME,
    ProgressEventType.WAITING: ActivityKind.RUNTIME,
    ProgressEventType.GIT_BASELINE_CAPTURED: ActivityKind.COMMAND,
    ProgressEventType.GIT_CHANGE_SET_VERIFIED: ActivityKind.COMMAND,
    ProgressEventType.GIT_COMMITTED: ActivityKind.COMMAND,
    ProgressEventType.GIT_NO_COMMIT_REQUIRED: ActivityKind.COMMAND,
}


@dataclass(frozen=True)
class ActivityEntry:
    at: datetime
    kind: ActivityKind
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.at, datetime):
            raise ValueError("activity timestamp must be a datetime")
        if not isinstance(self.kind, ActivityKind):
            raise ValueError("activity kind must be typed")
        if not isinstance(self.text, str):
            raise ValueError("activity text must be a string")


def _clip(value: str) -> str:
    return safe_text(value)[:MAX_ACTIVITY_TEXT]


class ActivityLog:
    """Bounded, scrollable history fed from persisted events and commands."""

    def __init__(self, *, limit: int = MAX_ACTIVITY_ENTRIES) -> None:
        self._entries: deque[ActivityEntry] = deque(maxlen=max(1, limit))
        self._seen: set[str] = set()
        self._offset = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[ActivityEntry, ...]:
        return tuple(self._entries)

    @property
    def offset(self) -> int:
        """Lines scrolled back from the newest entry (0 means following)."""

        return self._offset

    @property
    def following(self) -> bool:
        return self._offset == 0

    def scroll(self, lines: int, *, visible: int) -> None:
        """Move within the bounded history; new entries keep following at 0."""

        maximum = max(0, len(self._entries) - max(1, visible))
        self._offset = max(0, min(maximum, self._offset + lines))

    def scroll_to_latest(self) -> None:
        self._offset = 0

    def append(
        self, at: datetime, kind: ActivityKind, text: str, *, key: str | None = None
    ) -> bool:
        """Append one bounded line; return False when it is a duplicate."""

        if key is not None:
            if key in self._seen:
                return False
            self._seen.add(key)
        self._entries.append(ActivityEntry(at, kind, _clip(text)))
        return True

    def record_event(self, event: ProgressEvent) -> bool:
        """Project one progress event; never store payload or model output."""

        message = event.message or event.type.value
        task = "" if event.task_id is None else f"[{event.task_id}] "
        kind = _KIND_BY_EVENT.get(event.type, ActivityKind.INFO)
        key = f"{event.timestamp.isoformat()}|{event.type.value}|{event.task_id}|{message}"
        return self.append(event.timestamp, kind, f"{task}{message}", key=key)

    def visible(self, height: int) -> tuple[ActivityEntry, ...]:
        """Return the window to draw, honouring the current scroll offset."""

        height = max(1, height)
        entries = list(self._entries)
        end = max(0, len(entries) - self._offset)
        start = max(0, end - height)
        return tuple(entries[start:end])


__all__ = [
    "ActivityEntry",
    "ActivityKind",
    "ActivityLog",
    "MAX_ACTIVITY_ENTRIES",
    "MAX_ACTIVITY_TEXT",
]
