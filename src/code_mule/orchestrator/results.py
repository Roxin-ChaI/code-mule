"""Typed results produced by deterministic orchestration."""

from dataclasses import dataclass

from code_mule.domain.enums import ProjectStatus
from code_mule.domain.models import QualityStatus


@dataclass(frozen=True)
class ProjectStatusView:
    project_id: str
    project_name: str
    status: ProjectStatus
    active_plan_id: str | None
    current_task_id: str | None
    total_tasks: int
    completed_tasks: int
    in_progress_tasks: int
    pending_tasks: int
    blocked_tasks: int
    open_change_requests: int
    quality_status: QualityStatus | None


@dataclass(frozen=True)
class CommandResult:
    project_id: str
    previous_status: ProjectStatus
    current_status: ProjectStatus
    state_changed: bool
    event_id: str
    message: str


@dataclass(frozen=True)
class ChangeResult:
    project_id: str
    change_request_id: str
    previous_status: ProjectStatus
    current_status: ProjectStatus
    event_id: str


@dataclass(frozen=True)
class StopResult:
    project_id: str
    previous_status: ProjectStatus
    current_status: ProjectStatus
    state_changed: bool
    safe_point_required: bool
    event_ids: tuple[str, ...]
    message: str


__all__ = ["ChangeResult", "CommandResult", "ProjectStatusView", "StopResult"]
