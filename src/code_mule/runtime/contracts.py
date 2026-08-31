"""Typed contracts for one deterministic autonomous task cycle."""

from dataclasses import dataclass

from code_mule.domain.enums import SupervisorDecisionType
from code_mule.domain.models import Decision, ExecutionReport, Task


class TaskCycleError(RuntimeError):
    """Base class for deterministic task-cycle failures."""


class InvalidTaskCycleState(TaskCycleError):
    """Raised when persisted state does not satisfy cycle preconditions."""


class TaskCycleLimitReached(TaskCycleError):
    """Lower-level marker for a bounded REWORK limit."""


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


__all__ = [
    "InvalidTaskCycleState",
    "TaskCycleConfig",
    "TaskCycleError",
    "TaskCycleLimitReached",
    "TaskCycleOutcome",
    "TaskCycleRequest",
]
