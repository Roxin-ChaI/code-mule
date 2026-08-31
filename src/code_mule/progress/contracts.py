"""Typed ephemeral telemetry contracts for Code Mule runtimes."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProgressEventType(StrEnum):
    PROJECT_STARTED = "project.started"
    PROJECT_COMPLETED = "project.completed"
    PROJECT_STOPPED = "project.stopped"
    TASK_DISPATCHED = "task.dispatched"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_REWORK = "task.rework"
    TASK_HUMAN_REQUIRED = "task.human_required"
    WORKER_STARTING = "worker.starting"
    WORKER_STARTED = "worker.started"
    WORKER_ACTIVITY = "worker.activity"
    WORKER_COMPLETED = "worker.completed"
    WORKER_FAILED = "worker.failed"
    SUPERVISOR_REVIEW_STARTED = "supervisor.review_started"
    SUPERVISOR_REVIEW_COMPLETED = "supervisor.review_completed"
    SUPERVISOR_FAILED = "supervisor.failed"
    PLAN_COMPLETED = "plan.completed"
    WAITING = "runtime.waiting"
    HUMAN_GATE = "runtime.human_gate"
    ERROR = "runtime.error"


@dataclass(frozen=True)
class ProgressEvent:
    type: ProgressEventType
    timestamp: datetime
    project_id: str | None
    task_id: str | None
    attempt: int | None
    message: str | None
    metadata: dict[str, str]

    def __post_init__(self) -> None:
        if self.attempt is not None and self.attempt < 1:
            raise ValueError("attempt must be at least 1 when provided")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.metadata.items()
        ):
            raise TypeError("metadata keys and values must be strings")


__all__ = ["ProgressEvent", "ProgressEventType"]
