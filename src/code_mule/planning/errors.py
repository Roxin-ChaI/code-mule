"""Fail-closed, bounded errors for deterministic project planning."""

from enum import StrEnum
import re


class PlanningValidationCode(StrEnum):
    """Stable codes for every expected initial-PLAN validation boundary."""

    UNKNOWN = "unknown"
    DUPLICATE_ID = "duplicate_id"
    EXISTING_ID_COLLISION = "existing_id_collision"
    UNKNOWN_REQUIREMENT_REFERENCE = "unknown_requirement_reference"
    INACTIVE_REQUIREMENT_REFERENCE = "inactive_requirement_reference"
    EMPTY_REQUIREMENTS = "empty_requirements"
    EMPTY_MILESTONES = "empty_milestones"
    EMPTY_TASKS = "empty_tasks"
    EMPTY_MILESTONE = "empty_milestone"
    UNKNOWN_TASK_REFERENCE = "unknown_task_reference"
    TASK_MILESTONE_MEMBERSHIP = "task_milestone_membership"
    MISSING_ACCEPTANCE_CRITERIA = "missing_acceptance_criteria"
    MISSING_REQUIREMENT_REFERENCE = "missing_requirement_reference"
    INVALID_DEPENDENCY = "invalid_dependency"
    DEPENDENCY_CYCLE = "dependency_cycle"
    UNCOVERED_REQUIREMENT = "uncovered_requirement"
    MATERIALIZATION_STATE = "materialization_state"
    PLAN_ID_INVALID = "plan_id_invalid"
    PLAN_ID_COLLISION = "plan_id_collision"
    DOMAIN_CONSTRUCTION = "domain_construction"


_FIELD_PATH = re.compile(
    r"^[a-z][a-z0-9_]*(?:\[[0-9]+\])?(?:\.[a-z][a-z0-9_]*(?:\[[0-9]+\])?)*$"
)


class PlanningError(RuntimeError):
    """Base class for deterministic planning failures."""


class PlanningValidationError(PlanningError):
    """A safe initial-PLAN failure suitable for persisted audit metadata."""

    def __init__(
        self,
        safe_summary: str,
        *,
        code: PlanningValidationCode = PlanningValidationCode.UNKNOWN,
        field_path: str = "proposal",
    ) -> None:
        if not 1 <= len(safe_summary) <= 200 or any(
            marker in safe_summary.casefold()
            for marker in ("api_key", "password", "credential", "token=")
        ):
            raise ValueError("safe_summary is outside planning diagnostic bounds")
        if len(field_path) > 160 or _FIELD_PATH.fullmatch(field_path) is None:
            raise ValueError("field_path is outside planning diagnostic bounds")
        super().__init__(safe_summary)
        self.validation_code = code
        self.field_path = field_path
        self.safe_summary = safe_summary
        self.retryable = False


class InvalidPlanProposal(PlanningValidationError):
    """Raised when a Supervisor proposal cannot be safely accepted."""


class DuplicateProposalId(InvalidPlanProposal):
    """Raised when proposal entity identity is ambiguous."""


class UnknownProposalReference(InvalidPlanProposal):
    """Raised when a proposal references an unknown entity."""


class InvalidProposalDependency(InvalidPlanProposal):
    """Raised when a proposed dependency edge is invalid."""


class ProposalDependencyCycle(InvalidPlanProposal):
    """Raised when proposed Tasks contain a dependency cycle."""


class PlanMaterializationError(PlanningValidationError):
    """Raised when a validated proposal cannot form domain entities."""


class ProjectPlanningStateError(PlanningError):
    """Raised when the persisted Project is not eligible for initial planning."""


class SupervisorPlanningError(PlanningError):
    """Raised after a Supervisor PLAN failure is persisted for human review."""


def planning_validation_metadata(
    error: PlanningValidationError,
    *,
    operation: str = "plan",
    stage: str,
    failure_category: str = "deterministic_validation_failure",
) -> dict[str, str]:
    """Project only bounded typed fields; never exception or proposal text."""

    return {
        "operation": operation,
        "stage": stage,
        "error_type": type(error).__name__,
        "validation_code": error.validation_code.value,
        "field_path": error.field_path,
        "safe_summary": error.safe_summary,
        "failure_category": failure_category,
        "retryable": "false",
        "plan_created": "false",
        "worker_started": "false",
    }


__all__ = [
    "DuplicateProposalId",
    "InvalidPlanProposal",
    "InvalidProposalDependency",
    "PlanMaterializationError",
    "PlanningError",
    "PlanningValidationCode",
    "PlanningValidationError",
    "ProjectPlanningStateError",
    "ProposalDependencyCycle",
    "SupervisorPlanningError",
    "UnknownProposalReference",
    "planning_validation_metadata",
]
