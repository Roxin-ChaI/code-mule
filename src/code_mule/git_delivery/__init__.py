"""Public Git delivery contracts."""

from .contracts import (
    DirtyGitBaseline,
    EmptyGitChangeSet,
    GitBaseline,
    GitChangeSet,
    GitCommitError,
    GitCommitResult,
    GitDeliveryMode,
    GitDeliveryFailureCode,
    GitDeliveryFailureDetails,
    GitNoCommitResult,
    NoChangeDeliveryRecoveryEvidence,
    GitDeliveryError,
    GitOwnershipError,
    GitOwnershipStatus,
    GitStagingError,
    UnexpectedGitHead,
    WorkerVerificationError,
)
from .diagnostics import (
    git_delivery_failure_evidence,
    git_delivery_failure_metadata,
)
from .service import (
    GitCommandResult,
    GitCommandRunner,
    GitDeliveryService,
    commit_message,
    run_git_command,
)
from .isolation import GitWorkspaceIsolationError, register_state_exclusion

__all__ = [
    "DirtyGitBaseline",
    "EmptyGitChangeSet",
    "GitBaseline",
    "GitChangeSet",
    "GitCommitError",
    "GitCommitResult",
    "GitDeliveryMode",
    "GitDeliveryFailureCode",
    "GitDeliveryFailureDetails",
    "GitNoCommitResult",
    "NoChangeDeliveryRecoveryEvidence",
    "GitDeliveryError",
    "GitOwnershipError",
    "GitOwnershipStatus",
    "GitStagingError",
    "GitWorkspaceIsolationError",
    "UnexpectedGitHead",
    "WorkerVerificationError",
    "GitCommandResult",
    "GitCommandRunner",
    "GitDeliveryService",
    "commit_message",
    "run_git_command",
    "git_delivery_failure_evidence",
    "git_delivery_failure_metadata",
    "register_state_exclusion",
]
