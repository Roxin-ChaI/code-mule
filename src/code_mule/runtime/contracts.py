"""Typed contracts for one deterministic autonomous task cycle."""

from dataclasses import dataclass
from enum import StrEnum

from code_mule.domain.enums import ProjectStatus, SupervisorDecisionType
from code_mule.domain.models import Decision, ExecutionReport, Task


class TaskCycleError(RuntimeError):
    """Base class for deterministic task-cycle failures."""


class InvalidTaskCycleState(TaskCycleError):
    """Raised when persisted state does not satisfy cycle preconditions."""


class TaskCycleLimitReached(TaskCycleError):
    """Lower-level marker for a bounded REWORK limit."""


class ProjectExecutionError(RuntimeError):
    """Base class for deterministic project-execution failures."""


class InvalidProjectExecutionState(ProjectExecutionError):
    """Raised when persisted state violates project runtime invariants."""


class ProjectExecutionStopReason(StrEnum):
    PLAN_COMPLETED = "plan_completed"
    HUMAN_REQUIRED = "human_required"
    CHANGE_REQUESTED = "change_requested"
    PAUSED = "paused"
    TASK_LIMIT_REACHED = "task_limit_reached"
    NO_RUNNABLE_TASK = "no_runnable_task"
    TASK_CYCLE_STOPPED = "task_cycle_stopped"


@dataclass(frozen=True)
class TaskCycleConfig:
    max_attempts: int

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


@dataclass(frozen=True)
class TaskCycleRequest:
    task: Task
    initial_prompt: str

    def __post_init__(self) -> None:
        if self.initial_prompt == "":
            raise ValueError("initial_prompt must not be empty")


@dataclass(frozen=True)
class TaskCycleOutcome:
    task_id: str
    attempts: int
    final_decision: SupervisorDecisionType
    execution_reports: tuple[ExecutionReport, ...]
    decisions: tuple[Decision, ...]
    final_prompt: str | None
    human_action_required: bool


@dataclass(frozen=True)
class ProjectExecutionConfig:
    max_tasks_per_run: int

    def __post_init__(self) -> None:
        if self.max_tasks_per_run < 1:
            raise ValueError("max_tasks_per_run must be at least 1")


@dataclass(frozen=True)
class ProjectExecutionOutcome:
    project_id: str
    tasks_started: int
    tasks_completed: int
    task_ids: tuple[str, ...]
    final_project_status: ProjectStatus
    plan_completed: bool
    human_action_required: bool
    stop_reason: ProjectExecutionStopReason


__all__ = [
    "InvalidProjectExecutionState",
    "InvalidTaskCycleState",
    "ProjectExecutionConfig",
    "ProjectExecutionError",
    "ProjectExecutionOutcome",
    "ProjectExecutionStopReason",
    "TaskCycleConfig",
    "TaskCycleError",
    "TaskCycleLimitReached",
    "TaskCycleOutcome",
    "TaskCycleRequest",
]
