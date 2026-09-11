"""Fail-closed errors for deterministic change replanning."""

from enum import StrEnum


class ReplanFailureCode(StrEnum):
    """Bounded diagnostics for post-completion proposal/materialization failures."""

    UNKNOWN = "unknown"
    CHANGE_REQUEST_MISMATCH = "change_request_mismatch"
    ENTITY_ID_COLLISION = "entity_id_collision"
    HISTORICAL_TASK_MUTATION = "historical_task_mutation"
    HISTORICAL_MILESTONE_REUSE = "historical_milestone_reuse"
    AFFECTED_TASK_CLASSIFICATION = "affected_task_classification"
    NEW_TASK_REQUIRED = "new_task_required"
    NEW_MILESTONE_TASK_COVERAGE = "new_milestone_task_coverage"
    UNKNOWN_TASK_LINEAGE = "unknown_task_lineage"
    CONFLICTING_TASK_LINEAGE = "conflicting_task_lineage"
    NEW_TASK_DEPENDENCY_CYCLE = "new_task_dependency_cycle"
    UNSCHEDULABLE_PLAN = "unschedulable_plan"


class PostCompletionReplanningStage(StrEnum):
    """Safe lifecycle stage for a post-completion replanning failure."""

    IMPACT_ANALYSIS = "impact_analysis"
    PROPOSAL_VALIDATION = "proposal_validation"
    MATERIALIZATION = "materialization"


class ReplanningError(RuntimeError):
    """Base class for change-replanning failures."""


class InvalidReplanningState(ReplanningError):
    """Raised when persisted state cannot safely enter replanning."""


class InvalidReplanProposal(ReplanningError):
    """Raised when a Supervisor replan proposal is unsafe."""


class UnknownReplanReference(InvalidReplanProposal):
    """Raised when a proposal control field references an unknown ID."""


class ReplanIdCollision(InvalidReplanProposal):
    """Raised when a proposed entity ID is not new."""


class ConflictingTaskChange(InvalidReplanProposal):
    """Raised when one Task receives incompatible lifecycle changes."""


class InvalidReplanDependency(InvalidReplanProposal):
    """Raised when a replacement graph contains an invalid dependency."""


class ReplanDependencyCycle(InvalidReplanProposal):
    """Raised when a replacement graph contains a dependency cycle."""


class ReplanMaterializationError(ReplanningError):
    """Raised when a validated proposal cannot form a new Plan snapshot."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: ReplanFailureCode = ReplanFailureCode.UNKNOWN,
        field_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.field_path = field_path


class SupervisorReplanningError(ReplanningError):
    """Raised after a Supervisor failure is persisted for human review."""


__all__ = [
    "ConflictingTaskChange",
    "InvalidReplanDependency",
    "InvalidReplanProposal",
    "InvalidReplanningState",
    "ReplanDependencyCycle",
    "ReplanFailureCode",
    "ReplanIdCollision",
    "ReplanMaterializationError",
    "ReplanningError",
    "PostCompletionReplanningStage",
    "SupervisorReplanningError",
    "UnknownReplanReference",
]
