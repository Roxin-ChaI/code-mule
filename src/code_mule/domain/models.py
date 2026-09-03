"""Typed, storage-agnostic domain records for Code Mule."""

from dataclasses import dataclass
from datetime import datetime

from .enums import (
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    SupervisorDecisionType,
    TaskStatus,
)


def _require_non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


@dataclass
class Requirement:
    id: str
    project_id: str
    title: str
    description: str
    status: RequirementStatus
    priority: str
    acceptance_criteria: tuple[str, ...]
    introduced_by: str
    created_at: datetime
    updated_at: datetime
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.title, "title")


@dataclass
class Task:
    id: str
    milestone_id: str
    title: str
    description: str
    status: TaskStatus
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    execution_attempts: int
    created_at: datetime
    updated_at: datetime
    requirement_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.milestone_id, "milestone_id")
        _require_non_empty(self.title, "title")
        if self.execution_attempts < 0:
            raise ValueError("execution_attempts must be non-negative")


@dataclass
class Milestone:
    id: str
    plan_id: str
    title: str
    status: str
    task_ids: tuple[str, ...]


@dataclass
class Plan:
    id: str
    project_id: str
    version: int
    status: PlanStatus
    requirement_ids: tuple[str, ...]
    milestone_ids: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be at least 1")


@dataclass
class Project:
    id: str
    name: str
    status: ProjectStatus
    active_plan_id: str | None
    current_task_id: str | None
    created_at: datetime
    updated_at: datetime
    workspace: str | None = None
    objective: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.name, "name")
        if self.workspace == "":
            raise ValueError("workspace must not be empty")
        if self.objective == "":
            raise ValueError("objective must not be empty")


@dataclass
class ChangeRequest:
    id: str
    project_id: str
    description: str
    status: ChangeRequestStatus
    affected_requirement_ids: tuple[str, ...]
    created_by: str
    created_at: datetime


@dataclass
class ImpactAnalysis:
    change_request_id: str
    architecture_impact: str
    affected_components: tuple[str, ...]
    affected_completed_tasks: tuple[str, ...]
    affected_in_progress_tasks: tuple[str, ...]
    affected_pending_tasks: tuple[str, ...]
    tasks_to_add: tuple[str, ...]
    tasks_to_reopen: tuple[str, ...]
    tasks_to_cancel: tuple[str, ...]
    recommendation: str
    summary: str = ""
    affected_requirement_ids: tuple[str, ...] = ()
    affected_task_ids: tuple[str, ...] = ()
    requirements_to_add: tuple[str, ...] = ()
    requirements_to_update: tuple[str, ...] = ()
    milestone_ids: tuple[str, ...] = ()
    dependency_changes: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    rationale: str = ""


@dataclass
class Decision:
    id: str
    task_id: str | None
    type: SupervisorDecisionType
    rationale: str
    created_at: datetime


@dataclass
class ExecutionReport:
    id: str
    task_id: str
    attempt: int
    status: str
    files_changed: tuple[str, ...]
    tests: tuple[str, ...]
    static_checks: tuple[str, ...]
    git_state: str
    issues: tuple[str, ...]
    human_action_required: bool
    summary: str
    created_at: datetime


@dataclass
class QualityStatus:
    tests: str
    lint: str
    type_check: str
    build: str
    repository_clean: bool


@dataclass
class ProjectEvent:
    id: str
    project_id: str
    event_type: str
    entity_id: str | None
    timestamp: datetime
    metadata: dict[str, str]


@dataclass
class HumanAction:
    id: str
    project_id: str
    task_id: str | None
    category: HumanActionCategory
    summary: str
    requested_action: str
    risk: str
    status: HumanActionStatus
    created_at: datetime
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.requested_action, "requested_action")
        _require_non_empty(self.risk, "risk")
        if self.status is HumanActionStatus.PENDING and self.resolved_at is not None:
            raise ValueError("pending HumanAction cannot have resolved_at")
        if self.status is not HumanActionStatus.PENDING and self.resolved_at is None:
            raise ValueError("closed HumanAction requires resolved_at")


@dataclass
class HumanResolution:
    id: str
    action_id: str
    project_id: str
    strategy: HumanResolutionStrategy
    summary: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.action_id, "action_id")
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.summary, "summary")


__all__ = [
    "ChangeRequest",
    "Decision",
    "ExecutionReport",
    "ImpactAnalysis",
    "HumanAction",
    "HumanResolution",
    "Milestone",
    "Plan",
    "Project",
    "ProjectEvent",
    "QualityStatus",
    "Requirement",
    "Task",
]
