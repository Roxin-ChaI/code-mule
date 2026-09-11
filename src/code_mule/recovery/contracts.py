"""Typed persistence and deterministic recovery contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re


class ExecutionStopReason(StrEnum):
    BOSS_PAUSE = "boss_pause"
    BOSS_STOP = "boss_stop"
    HUMAN_REQUIRED = "human_required"
    WORKER_INPUT = "worker_input"
    WORKER_VERIFICATION = "worker_verification"
    WORKSPACE_BLOCK = "workspace_block"
    RECOVERY_UNCERTAIN = "recovery_uncertain"
    WORKER_INTERRUPTED = "worker_interrupted"
    WORKER_TIMEOUT = "worker_timeout"
    WORKER_FAILED = "worker_failed"
    SUPERVISOR_FAILED = "supervisor_failed"
    ATTEMPT_LIMIT = "attempt_limit"
    CHANGE_REQUESTED = "change_requested"
    PLANNING_INTERRUPTED = "planning_interrupted"
    EXECUTION_COMPLETED = "execution_completed"


class ExecutionPhase(StrEnum):
    PROJECT = "project"
    PLANNING = "planning"
    REPLANNING = "replanning"
    EXECUTION = "execution"
    WORKER = "worker"
    REVIEW = "review"
    DELIVERY = "delivery"
    FINALIZATION = "finalization"
    CONTROL = "control"


class SafePointKind(StrEnum):
    PROJECT_IDLE = "project_idle"
    PLAN_MATERIALIZED = "plan_materialized"
    TASK_READY = "task_ready"
    TASK_BASELINE_CAPTURED = "task_baseline_captured"
    TASK_WORKER_COMPLETED = "task_worker_completed"
    TASK_DELIVERED = "task_delivered"
    HUMAN_GATE = "human_gate"
    PROJECT_FINALIZING = "project_finalizing"
    PROJECT_DONE = "project_done"
    UNCERTAIN = "uncertain"


class BoundaryRecoverability(StrEnum):
    RECOVERABLE = "recoverable"
    UNCERTAIN = "uncertain"
    TERMINAL = "terminal"
    NOT_APPLICABLE = "not_applicable"


class WorkerTerminalState(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class ExecutionAttemptStatus(StrEnum):
    PREPARED = "prepared"
    WORKER_STARTED = "worker_started"
    WORKER_COMPLETED = "worker_completed"
    REPORT_PERSISTED = "report_persisted"
    REVIEW_STARTED = "review_started"
    REVIEW_COMPLETED = "review_completed"
    DELIVERY_STARTED = "delivery_started"
    DELIVERED = "delivered"
    INTERRUPTED = "interrupted"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class RecoveryMode(StrEnum):
    RESUME_PLAN = "resume_plan"
    DISPATCH_FRESH_WORKER = "dispatch_fresh_worker"
    CONTINUE_AFTER_REPORT = "continue_after_report"
    CONTINUE_AFTER_INPUT = "continue_after_input"
    FRESH_PLANNING = "fresh_planning"
    FRESH_REPLANNING = "fresh_replanning"
    BLOCKED = "blocked"
    NOT_NEEDED = "not_needed"


def _optional_id(value: str | None, name: str) -> None:
    if value is not None and not re.fullmatch(r"[^\s\x00-\x1f]{1,128}", value):
        raise ValueError(f"{name} must be a bounded identifier")


def _sha(value: str | None) -> None:
    if value is not None and not re.fullmatch(r"[0-9a-fA-F]{7,128}", value):
        raise ValueError("head_sha must be a bounded Git identifier")


@dataclass(frozen=True)
class SafePoint:
    kind: SafePointKind
    recorded_at: datetime
    task_id: str | None = None
    attempt: int | None = None
    head_sha: str | None = None

    def __post_init__(self) -> None:
        _optional_id(self.task_id, "task_id")
        _sha(self.head_sha)
        if self.attempt is not None and self.attempt < 1:
            raise ValueError("attempt must be positive")
        if self.attempt is not None and self.task_id is None:
            raise ValueError("attempt requires task_id")


@dataclass(frozen=True)
class ExecutionStopBoundary:
    reason: ExecutionStopReason
    phase: ExecutionPhase
    safe_point: SafePointKind
    recoverability: BoundaryRecoverability
    worker_started: bool
    worker_terminal_state: WorkerTerminalState
    report_persisted: bool
    recorded_at: datetime
    task_id: str | None = None
    attempt: int | None = None
    head_sha: str | None = None

    def __post_init__(self) -> None:
        _optional_id(self.task_id, "task_id")
        _sha(self.head_sha)
        if self.attempt is not None and self.attempt < 1:
            raise ValueError("attempt must be positive")
        if self.attempt is not None and self.task_id is None:
            raise ValueError("attempt requires task_id")
        if self.report_persisted and self.worker_terminal_state is not WorkerTerminalState.COMPLETED:
            raise ValueError("persisted report requires completed Worker")
        if not self.worker_started and self.worker_terminal_state is not WorkerTerminalState.NOT_STARTED:
            raise ValueError("unstarted Worker cannot have a terminal state")


@dataclass(frozen=True)
class ExecutionAttempt:
    task_id: str
    attempt: int
    status: ExecutionAttemptStatus
    started_at: datetime
    thread_id: str | None = None
    turn_id: str | None = None
    baseline_head: str | None = None
    terminal_at: datetime | None = None
    failure_kind: str | None = None
    partial_paths_exist: bool = False

    def __post_init__(self) -> None:
        _optional_id(self.task_id, "task_id")
        _optional_id(self.thread_id, "thread_id")
        _optional_id(self.turn_id, "turn_id")
        _sha(self.baseline_head)
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        if self.terminal_at is not None and self.terminal_at < self.started_at:
            raise ValueError("terminal_at cannot precede started_at")
        if self.failure_kind is not None and not re.fullmatch(
            r"[a-z0-9_.-]{1,64}", self.failure_kind
        ):
            raise ValueError("failure_kind must be normalized and bounded")


@dataclass(frozen=True)
class RecoveryPlan:
    recovery_mode: RecoveryMode
    from_safe_point: SafePointKind
    recoverability: BoundaryRecoverability
    requires_boss_action: bool
    requires_workspace_validation: bool
    fresh_worker_required: bool
    automatic_resume_allowed: bool
    reason: str
    next_command: str
    task_id: str | None = None
    attempt: int | None = None

    def __post_init__(self) -> None:
        _optional_id(self.task_id, "task_id")
        if self.attempt is not None and (self.attempt < 1 or self.task_id is None):
            raise ValueError("attempt requires a Task and must be positive")
        for name, value, limit in (
            ("reason", self.reason, 300),
            ("next_command", self.next_command, 120),
        ):
            if not value or len(value) > limit or "\n" in value:
                raise ValueError(f"{name} must be bounded")


__all__ = [
    "BoundaryRecoverability",
    "ExecutionAttempt",
    "ExecutionAttemptStatus",
    "ExecutionPhase",
    "ExecutionStopBoundary",
    "ExecutionStopReason",
    "RecoveryMode",
    "RecoveryPlan",
    "SafePoint",
    "SafePointKind",
    "WorkerTerminalState",
]
