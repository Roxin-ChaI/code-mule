"""Typed inputs and outcomes for deterministic change replanning."""

from dataclasses import dataclass

from code_mule.domain.enums import ProjectStatus
from code_mule.runtime.contracts import ProjectExecutionOutcome


def _require_non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True)
class ChangeReplanningRequest:
    project_id: str
    change_request_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.change_request_id, "change_request_id")


@dataclass(frozen=True)
class ChangeReplanningOutcome:
    project_id: str
    change_request_id: str
    previous_plan_id: str
    plan_id: str
    previous_plan_version: int
    plan_version: int
    project_status: ProjectStatus
    reopened_task_ids: tuple[str, ...]
    cancelled_task_ids: tuple[str, ...]
    added_task_ids: tuple[str, ...]
    ready_for_execution: bool


@dataclass(frozen=True)
class ChangeExecutionOutcome:
    replanning: ChangeReplanningOutcome
    execution: ProjectExecutionOutcome | None
    final_project_status: ProjectStatus
    human_action_required: bool


__all__ = [
    "ChangeExecutionOutcome",
    "ChangeReplanningOutcome",
    "ChangeReplanningRequest",
]
