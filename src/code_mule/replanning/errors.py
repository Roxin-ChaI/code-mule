"""Fail-closed errors for deterministic change replanning."""


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


class SupervisorReplanningError(ReplanningError):
    """Raised after a Supervisor failure is persisted for human review."""


__all__ = [
    "ConflictingTaskChange",
    "InvalidReplanDependency",
    "InvalidReplanProposal",
    "InvalidReplanningState",
    "ReplanDependencyCycle",
    "ReplanIdCollision",
    "ReplanMaterializationError",
    "ReplanningError",
    "SupervisorReplanningError",
    "UnknownReplanReference",
]
