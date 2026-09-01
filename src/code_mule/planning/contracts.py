"""Typed input and output contracts for initial project planning."""

from dataclasses import dataclass

from code_mule.domain.enums import ProjectStatus
from code_mule.runtime.contracts import ProjectExecutionOutcome


def _require_non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True)
class ProjectPlanningRequest:
    project_id: str
    objective: str

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.objective, "objective")


@dataclass(frozen=True)
class ProjectPlanningOutcome:
    project_id: str
    plan_id: str
    plan_version: int
    requirement_ids: tuple[str, ...]
    milestone_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    project_status: ProjectStatus
    ready_for_execution: bool


@dataclass(frozen=True)
class AutonomousProjectOutcome:
    planning: ProjectPlanningOutcome
    execution: ProjectExecutionOutcome | None
    final_project_status: ProjectStatus
    human_action_required: bool


__all__ = [
    "AutonomousProjectOutcome",
    "ProjectPlanningOutcome",
    "ProjectPlanningRequest",
]
