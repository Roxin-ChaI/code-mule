"""Typed ephemeral telemetry contracts for Code Mule runtimes."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProgressEventType(StrEnum):
    CHANGE_REQUESTED = "change.requested"
    REPLANNING_STARTED = "replanning.started"
    SUPERVISOR_IMPACT_STARTED = "supervisor.impact_started"
    SUPERVISOR_IMPACT_COMPLETED = "supervisor.impact_completed"
    REPLANNING_MATERIALIZING = "replanning.materializing"
    REPLANNING_COMPLETED = "replanning.completed"
    REPLANNING_FAILED = "replanning.failed"
    PLANNING_STARTED = "planning.started"
    SUPERVISOR_PLAN_STARTED = "supervisor.plan_started"
    SUPERVISOR_PLAN_COMPLETED = "supervisor.plan_completed"
    PLANNING_MATERIALIZING = "planning.materializing"
    PLANNING_COMPLETED = "planning.completed"
    PLANNING_FAILED = "planning.failed"
    PROJECT_STARTED = "project.started"
    PROJECT_COMPLETED = "project.completed"
    PROJECT_VERIFICATION_STARTED = "project.verification_started"
    PROJECT_VERIFICATION_COMPLETED = "project.verification_completed"
    PROJECT_FINAL_REVIEW_STARTED = "project.final_review_started"
    PROJECT_FINAL_REVIEW_COMPLETED = "project.final_review_completed"
    PROJECT_STOPPED = "project.stopped"
    PROJECT_CANCELLATION_REQUESTED = "project.cancel_requested"
    PROJECT_CANCELLED = "project.cancelled"
    TASK_DISPATCHED = "task.dispatched"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_REWORK = "task.rework"
    TASK_HUMAN_REQUIRED = "task.human_required"
    GIT_BASELINE_CAPTURED = "git.baseline_captured"
    GIT_CHANGE_SET_VERIFIED = "git.change_set_verified"
    GIT_COMMITTED = "git.committed"
    GIT_DELIVERY_FAILED = "git.delivery_failed"
    WORKER_STARTING = "worker.starting"
    WORKER_STARTED = "worker.started"
    WORKER_ACTIVITY = "worker.activity"
    WORKER_COMPLETED = "worker.completed"
    WORKER_FAILED = "worker.failed"
    SUPERVISOR_REVIEW_STARTED = "supervisor.review_started"
    SUPERVISOR_REVIEW_COMPLETED = "supervisor.review_completed"
    SUPERVISOR_FAILED = "supervisor.failed"
    SUPERVISOR_RETRYING = "supervisor.retrying"
    SUPERVISOR_RETRY_SUCCEEDED = "supervisor.retry_succeeded"
    SUPERVISOR_RETRY_EXHAUSTED = "supervisor.retry_exhausted"
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
    project_name: str | None = None
    plan_version: int | None = None
    worker_activity: str | None = None

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
