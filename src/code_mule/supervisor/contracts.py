"""Typed requests and proposals at the Supervisor reasoning boundary."""

from dataclasses import dataclass
from enum import StrEnum

from code_mule.domain.enums import SupervisorDecisionType
from code_mule.domain.models import ChangeRequest, ExecutionReport, Task
from code_mule.state.models import ProjectState


def _require_non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


class SupervisorOperation(StrEnum):
    PLAN = "plan"
    REVIEW = "review"
    IMPACT_ANALYSIS = "impact_analysis"
    PROGRESS_REPORT = "progress_report"
    BOSS_ROUTING = "boss_routing"


class SupervisorFailureCategory(StrEnum):
    TRANSPORT_TIMEOUT = "transport_timeout"
    TEMPORARY_CONNECTION_FAILURE = "temporary_connection_failure"
    INCOMPLETE_MAX_OUTPUT_TOKENS = "incomplete_max_output_tokens"
    MALFORMED_STRUCTURED_RESPONSE = "malformed_structured_response"
    SCHEMA_CONTRACT_VIOLATION = "schema_contract_violation"
    DECISION_CONTRACT_VIOLATION = "decision_contract_violation"
    CONTENT_FILTER = "content_filter"
    DETERMINISTIC_VALIDATION_FAILURE = "deterministic_validation_failure"
    HUMAN_GATE = "human_gate"
    INVALID_BUSINESS_REFERENCE = "invalid_business_reference"
    PROVIDER_AUTHENTICATION = "provider_authentication"
    PROVIDER_CONFIGURATION = "provider_configuration"
    UNKNOWN_FAILURE = "unknown_failure"


_RETRYABLE_FAILURES = frozenset(
    {
        SupervisorFailureCategory.TRANSPORT_TIMEOUT,
        SupervisorFailureCategory.TEMPORARY_CONNECTION_FAILURE,
        SupervisorFailureCategory.INCOMPLETE_MAX_OUTPUT_TOKENS,
        SupervisorFailureCategory.MALFORMED_STRUCTURED_RESPONSE,
        SupervisorFailureCategory.SCHEMA_CONTRACT_VIOLATION,
        SupervisorFailureCategory.DECISION_CONTRACT_VIOLATION,
    }
)


def supervisor_failure_is_retryable(
    category: SupervisorFailureCategory,
) -> bool:
    return category in _RETRYABLE_FAILURES


@dataclass(frozen=True)
class SupervisorRetryPolicy:
    max_attempts: int = 2
    retry_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")


@dataclass(frozen=True)
class SupervisorAttemptResult:
    operation: SupervisorOperation
    attempt: int
    succeeded: bool
    failure_category: SupervisorFailureCategory | None
    retryable: bool

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        if self.succeeded:
            if self.failure_category is not None or self.retryable:
                raise ValueError("successful attempt cannot carry failure metadata")
        elif self.failure_category is None:
            raise ValueError("failed attempt requires failure_category")


class SupervisorCallFailure(RuntimeError):
    """Safe typed result of a non-retryable or exhausted Supervisor call."""

    def __init__(
        self,
        *,
        operation: SupervisorOperation,
        failure_category: SupervisorFailureCategory,
        attempt_count: int,
        retryable: bool,
        exhausted: bool,
    ) -> None:
        if attempt_count < 1:
            raise ValueError("attempt_count must be at least 1")
        super().__init__(
            f"Supervisor {operation.value} failed after {attempt_count} "
            f"attempt(s): {failure_category.value}"
        )
        self.operation = operation
        self.failure_category = failure_category
        self.attempt_count = attempt_count
        self.retryable = retryable
        self.exhausted = exhausted


@dataclass(frozen=True)
class PlanRequest:
    project_state: ProjectState
    objective: str

    def __post_init__(self) -> None:
        _require_non_empty(self.objective, "objective")


@dataclass(frozen=True)
class ReviewRequest:
    project_state: ProjectState
    task: Task
    execution_report: ExecutionReport


@dataclass(frozen=True)
class ImpactAnalysisRequest:
    project_state: ProjectState
    change_request: ChangeRequest


@dataclass(frozen=True)
class ProgressReportRequest:
    project_state: ProjectState
    question: str | None

    def __post_init__(self) -> None:
        if self.question == "":
            raise ValueError("question must not be empty when provided")


@dataclass(frozen=True)
class RequirementProposal:
    id: str
    title: str
    description: str
    priority: str
    acceptance_criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.title, "title")
        _require_non_empty(self.description, "description")
        _require_non_empty(self.priority, "priority")
        if not self.acceptance_criteria:
            raise ValueError("acceptance_criteria must contain at least one item")


@dataclass(frozen=True)
class RequirementUpdateProposal:
    supersedes_id: str
    requirement: RequirementProposal

    def __post_init__(self) -> None:
        _require_non_empty(self.supersedes_id, "supersedes_id")


@dataclass(frozen=True)
class TaskProposal:
    id: str
    title: str
    description: str
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    requirement_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.title, "title")
        _require_non_empty(self.description, "description")
        if not self.acceptance_criteria:
            raise ValueError("acceptance_criteria must contain at least one item")
        if not self.requirement_ids:
            raise ValueError("requirement_ids must contain at least one item")


@dataclass(frozen=True)
class MilestoneProposal:
    id: str
    title: str
    task_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "id")
        _require_non_empty(self.title, "title")


@dataclass(frozen=True)
class TaskDependencyChange:
    task_id: str
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "task_id")


@dataclass(frozen=True)
class TaskRequirementUpdate:
    task_id: str
    requirement_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "task_id")
        if not self.requirement_ids:
            raise ValueError("requirement_ids must contain at least one item")


@dataclass(frozen=True)
class PlanProposal:
    summary: str
    requirements: tuple[RequirementProposal, ...]
    requirements_considered: tuple[str, ...]
    milestones: tuple[MilestoneProposal, ...]
    tasks: tuple[TaskProposal, ...]
    risks: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.rationale, "rationale")


@dataclass(frozen=True)
class ReviewResult:
    decision: SupervisorDecisionType
    rationale: str
    next_task_prompt: str | None
    issues: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.decision is SupervisorDecisionType.REWORK:
            if self.next_task_prompt in (None, ""):
                raise ValueError("REWORK requires a non-empty next_task_prompt")
        elif self.decision in {
            SupervisorDecisionType.HUMAN_REQUIRED,
            SupervisorDecisionType.DONE,
        }:
            if self.next_task_prompt is not None:
                raise ValueError(
                    f"{self.decision.name} requires next_task_prompt to be None"
                )


@dataclass(frozen=True)
class ImpactAnalysisResult:
    change_request_id: str
    summary: str
    architecture_impact: str
    affected_components: tuple[str, ...]
    affected_requirement_ids: tuple[str, ...]
    affected_task_ids: tuple[str, ...]
    affected_completed_tasks: tuple[str, ...]
    affected_in_progress_tasks: tuple[str, ...]
    affected_pending_tasks: tuple[str, ...]
    requirements_to_add: tuple[RequirementProposal, ...]
    requirements_to_update: tuple[RequirementUpdateProposal, ...]
    tasks_to_add: tuple[TaskProposal, ...]
    tasks_to_reopen: tuple[str, ...]
    tasks_to_cancel: tuple[str, ...]
    milestone_ids_reused: tuple[str, ...]
    milestones: tuple[MilestoneProposal, ...]
    dependency_changes: tuple[TaskDependencyChange, ...]
    task_requirement_updates: tuple[TaskRequirementUpdate, ...]
    risks: tuple[str, ...]
    recommendation: str
    rationale: str

    def __post_init__(self) -> None:
        _require_non_empty(self.change_request_id, "change_request_id")
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.recommendation, "recommendation")
        _require_non_empty(self.rationale, "rationale")


@dataclass(frozen=True)
class ProgressReport:
    summary: str
    current_status: str
    current_work: str | None
    completed: tuple[str, ...]
    remaining: tuple[str, ...]
    blockers: tuple[str, ...]
    risks: tuple[str, ...]
    quality_summary: str | None


__all__ = [
    "ImpactAnalysisRequest",
    "ImpactAnalysisResult",
    "MilestoneProposal",
    "PlanProposal",
    "PlanRequest",
    "ProgressReport",
    "ProgressReportRequest",
    "RequirementProposal",
    "RequirementUpdateProposal",
    "ReviewRequest",
    "ReviewResult",
    "SupervisorOperation",
    "SupervisorAttemptResult",
    "SupervisorCallFailure",
    "SupervisorFailureCategory",
    "SupervisorRetryPolicy",
    "TaskProposal",
    "TaskDependencyChange",
    "TaskRequirementUpdate",
    "supervisor_failure_is_retryable",
]
