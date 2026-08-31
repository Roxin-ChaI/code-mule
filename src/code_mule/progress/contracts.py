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


@dataclass(frozen=True)
class ProgressSnapshot:
    project_id: str | None
    project_status: str | None
    completed_tasks: int
    total_tasks: int
    current_task_id: str | None
    current_task_title: str | None
    current_attempt: int | None
    worker_status: str
    supervisor_status: str
    project_started_at: datetime | None
    task_started_at: datetime | None
    stage_started_at: datetime | None
    recent_events: tuple[ProgressEvent, ...]

    @property
    def percentage(self) -> float:
        return progress_percentage(self.completed_tasks, self.total_tasks)


def progress_percentage(completed_tasks: int, total_tasks: int) -> float:
    if completed_tasks < 0 or total_tasks < 0:
        raise ValueError("task counts must be non-negative")
    if completed_tasks > total_tasks:
        raise ValueError("completed_tasks must not exceed total_tasks")
    if total_tasks == 0:
        return 0.0
    return completed_tasks / total_tasks * 100.0


__all__ = [
    "ProgressEvent",
    "ProgressEventType",
    "ProgressSnapshot",
    "progress_percentage",
]
