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
    "TaskProposal",
    "TaskDependencyChange",
]
