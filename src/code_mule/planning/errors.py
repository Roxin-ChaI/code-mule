"""Fail-closed errors for deterministic project planning."""


class PlanningError(RuntimeError):
    """Base class for deterministic planning failures."""


class InvalidPlanProposal(PlanningError):
    """Raised when a Supervisor proposal cannot be safely accepted."""


class DuplicateProposalId(InvalidPlanProposal):
    """Raised when proposal entity identity is ambiguous."""


class UnknownProposalReference(InvalidPlanProposal):
    """Raised when a proposal references an unknown entity."""


class InvalidProposalDependency(InvalidPlanProposal):
    """Raised when a proposed dependency edge is invalid."""


class ProposalDependencyCycle(InvalidPlanProposal):
    """Raised when proposed Tasks contain a dependency cycle."""


class PlanMaterializationError(PlanningError):
    """Raised when a validated proposal cannot form domain entities."""


class ProjectPlanningStateError(PlanningError):
    """Raised when the persisted Project is not eligible for initial planning."""


class SupervisorPlanningError(PlanningError):
    """Raised after a Supervisor PLAN failure is persisted for human review."""


__all__ = [
    "DuplicateProposalId",
    "InvalidPlanProposal",
    "InvalidProposalDependency",
    "PlanMaterializationError",
    "PlanningError",
    "ProjectPlanningStateError",
    "ProposalDependencyCycle",
    "SupervisorPlanningError",
    "UnknownProposalReference",
]
