"""Typed, storage-agnostic domain records for Code Mule."""

from dataclasses import dataclass
from datetime import datetime
from .worker_verification import WorkerVerificationCheck

from .enums import (
    CapabilityApprovalScope,
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    RevisionCheckStatus,
    RevisionStatus,
    SupervisorDecisionType,
    TaskStatus,
    WorkerHumanActionKind,
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
    supersedes_task_id: str | None = None
    derived_from_task_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.milestone_id, "milestone_id")
        _require_non_empty(self.title, "title")
        if self.execution_attempts < 0:
            raise ValueError("execution_attempts must be non-negative")
        if self.supersedes_task_id == "":
            raise ValueError("supersedes_task_id must not be empty")
        if self.supersedes_task_id == self.id:
            raise ValueError("task cannot supersede itself")
        if self.id in self.derived_from_task_ids:
            raise ValueError("task cannot derive from itself")
        if self.supersedes_task_id is not None and (
            self.supersedes_task_id in self.derived_from_task_ids
        ):
            raise ValueError("supersedes_task_id conflicts with derived lineage")


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
    base_plan_id: str | None = None
    base_plan_version: int | None = None
    change_request_id: str | None = None
    revision_number: int | None = None
    reused_task_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be at least 1")
        if (self.base_plan_id is None) != (self.base_plan_version is None):
            raise ValueError(
                "base_plan_id and base_plan_version must be provided together"
            )
        if self.base_plan_version is not None and self.base_plan_version < 1:
            raise ValueError("base_plan_version must be at least 1")
        if self.revision_number is not None and self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        if self.base_plan_id == "":
            raise ValueError("base_plan_id must not be empty")


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
    requested_revision: int | None = None
    base_revision: int | None = None
    base_plan_id: str | None = None
    base_plan_version: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.description, "description")
        _require_non_empty(self.created_by, "created_by")
        if self.requested_revision is not None and self.requested_revision < 1:
            raise ValueError("requested_revision must be at least 1")
        if self.base_revision is not None and self.base_revision < 1:
            raise ValueError("base_revision must be at least 1")
        if self.base_plan_version is not None and self.base_plan_version < 1:
            raise ValueError("base_plan_version must be at least 1")
        if (self.base_plan_id is None) != (self.base_plan_version is None):
            raise ValueError(
                "base_plan_id and base_plan_version must be provided together"
            )


@dataclass
class ProjectRevision:
    """One immutable historical execution revision of a project."""

    revision_number: int
    started_at: datetime
    lifecycle_status: RevisionStatus = RevisionStatus.IN_PROGRESS
    plan_id: str | None = None
    plan_version: int | None = None
    base_revision: int | None = None
    change_request_id: str | None = None
    completed_at: datetime | None = None
    baseline_head: str | None = None
    completion_head: str | None = None
    verification_status: RevisionCheckStatus = RevisionCheckStatus.NOT_RUN
    final_review_status: RevisionCheckStatus = RevisionCheckStatus.NOT_RUN
    verification_result_id: str | None = None

    def __post_init__(self) -> None:
        if self.revision_number < 1:
            raise ValueError("revision_number must be at least 1")
        if self.plan_version is not None and self.plan_version < 1:
            raise ValueError("plan_version must be at least 1")
        if self.base_revision is not None and self.base_revision < 1:
            raise ValueError("base_revision must be at least 1")
        if self.plan_id == "":
            raise ValueError("plan_id must not be empty")
        if self.change_request_id == "":
            raise ValueError("change_request_id must not be empty")
        if self.baseline_head == "" or self.completion_head == "":
            raise ValueError("Git head must not be empty")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")


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


@dataclass(frozen=True)
class WorkerHumanAction:
    kind: WorkerHumanActionKind
    summary: str
    request: str
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WorkerHumanActionKind):
            raise ValueError("Worker human action kind must be typed")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("Worker human action summary must be nonblank")
        if not isinstance(self.request, str) or not self.request.strip():
            raise ValueError("Worker human action request must be nonblank")
        if not isinstance(self.choices, tuple) or any(
            not isinstance(choice, str) or not choice.strip() for choice in self.choices
        ):
            raise ValueError("Worker human action choices must be nonblank strings")
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.request, "request")
        if len(self.summary) > 1_000 or len(self.request) > 2_000:
            raise ValueError("Worker human action exceeds safe bounds")
        if len(self.choices) > 20 or any(
            choice == "" or len(choice) > 500 for choice in self.choices
        ):
            raise ValueError("Worker human action choices exceed safe bounds")
        if self.kind is not WorkerHumanActionKind.INPUT and self.choices:
            raise ValueError("choices are only valid for input actions")


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
    human_action: WorkerHumanAction | None
    summary: str
    created_at: datetime
    # None is explicit legacy provenance, treated conservatively as required checks.
    verification_checks: tuple[WorkerVerificationCheck, ...] | None = None

    def __post_init__(self) -> None:
        if self.human_action is not None and not isinstance(self.human_action, WorkerHumanAction):
            raise ValueError("human_action must be a typed WorkerHumanAction or None")
        if self.verification_checks is not None and (
            not isinstance(self.verification_checks, tuple)
            or not all(isinstance(check, WorkerVerificationCheck) for check in self.verification_checks)
        ):
            raise ValueError("verification_checks must contain typed checks")

    @property
    def human_action_required(self) -> bool:
        return self.human_action is not None


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
class WorkerInputDetails:
    request_method: str
    request_id: str | None
    question: str
    choices: tuple[str, ...]
    worker_attempt: int
    baseline_head: str | None = None
    partial_paths: tuple[str, ...] = ()
    answer: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.request_method, "request_method")
        _require_non_empty(self.question, "question")
        if len(self.request_method) > 128 or len(self.question) > 2_000:
            raise ValueError("Worker input request exceeds safe bounds")
        if self.request_id is not None:
            _require_non_empty(self.request_id, "request_id")
            if len(self.request_id) > 128:
                raise ValueError("request_id exceeds safe bounds")
        if self.worker_attempt < 1:
            raise ValueError("worker_attempt must be positive")
        if len(self.choices) > 20 or any(
            choice == "" or len(choice) > 500 for choice in self.choices
        ):
            raise ValueError("choices exceed safe bounds")
        if len(self.partial_paths) > 1_000 or any(
            path == "" or len(path) > 1_024 for path in self.partial_paths
        ):
            raise ValueError("partial_paths exceed safe bounds")
        if self.baseline_head is not None:
            _require_non_empty(self.baseline_head, "baseline_head")
            if len(self.baseline_head) > 128:
                raise ValueError("baseline_head exceeds safe bounds")
        if self.answer is not None:
            _require_non_empty(self.answer, "answer")
            if len(self.answer) > 4_000:
                raise ValueError("answer exceeds safe bounds")


@dataclass
class WorkerCapabilityApprovalDetails:
    """Safe persisted projection of one native Codex approval request.

    ``request_method`` distinguishes sandbox/tool requests from MCP capability
    requests.  For native sandbox requests, ``tool_name`` is the bounded command
    identity, ``application`` is the structured purpose when supplied, and
    ``capability_id`` is the structured target/policy when supplied.
    """

    request_method: str
    request_id: str
    thread_id: str
    turn_id: str
    server_name: str
    capability: str
    application: str | None
    capability_id: str | None
    tool_name: str | None
    approval_scopes: tuple[CapabilityApprovalScope, ...]
    worker_attempt: int
    baseline_head: str | None = None
    partial_paths: tuple[str, ...] = ()
    native_request_active: bool = False

    def __post_init__(self) -> None:
        required = {
            "request_method": (self.request_method, 128),
            "request_id": (self.request_id, 128),
            "thread_id": (self.thread_id, 128),
            "turn_id": (self.turn_id, 128),
            "server_name": (self.server_name, 200),
            "capability": (self.capability, 200),
        }
        optional = {
            "application": (self.application, 200),
            "capability_id": (self.capability_id, 200),
            "tool_name": (self.tool_name, 200),
            "baseline_head": (self.baseline_head, 128),
        }
        for name, (value, limit) in required.items():
            _require_non_empty(value, name)
            if len(value) > limit:
                raise ValueError(f"{name} exceeds safe bounds")
        for name, (value, limit) in optional.items():
            if value is not None:
                _require_non_empty(value, name)
                if len(value) > limit:
                    raise ValueError(f"{name} exceeds safe bounds")
        if self.worker_attempt < 1:
            raise ValueError("worker_attempt must be positive")
        if len(self.approval_scopes) > 3 or len(set(self.approval_scopes)) != len(
            self.approval_scopes
        ):
            raise ValueError("approval_scopes must be unique and bounded")
        if any(not isinstance(scope, CapabilityApprovalScope) for scope in self.approval_scopes):
            raise ValueError("approval_scopes must be typed")
        if len(self.partial_paths) > 1_000 or any(
            path == "" or len(path) > 1_024 for path in self.partial_paths
        ):
            raise ValueError("partial_paths exceed safe bounds")
        if type(self.native_request_active) is not bool:
            raise ValueError("native_request_active must be boolean")

    @property
    def identity(self) -> tuple[str, str, str, str, str, str]:
        """Structured identity used for deterministic duplicate-loop detection."""

        return (
            self.request_method,
            self.server_name,
            self.capability_id or "",
            self.tool_name or "",
            self.capability,
            self.application or "",
        )


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
    worker_input: WorkerInputDetails | None = None
    capability_approval: WorkerCapabilityApprovalDetails | None = None

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
        if (
            self.worker_input is not None
            and self.category is not HumanActionCategory.WORKER_INPUT
        ):
            raise ValueError("worker_input is only valid for WORKER_INPUT actions")
        if self.worker_input is not None and self.capability_approval is not None:
            raise ValueError("HumanAction cannot contain two Worker request types")
        if self.capability_approval is not None and self.category not in {
            HumanActionCategory.WORKER_APPROVAL,
            HumanActionCategory.RECOVERY_UNCERTAIN,
        }:
            raise ValueError(
                "capability_approval is only valid for approval or recovery actions"
            )


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
    "ProjectRevision",
    "QualityStatus",
    "Requirement",
    "Task",
    "WorkerInputDetails",
    "WorkerCapabilityApprovalDetails",
    "WorkerHumanAction",
]
