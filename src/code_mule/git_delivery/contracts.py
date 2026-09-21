"""Typed contracts for deterministic per-Task Git delivery."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from code_mule.domain.worker_verification import WorkerVerificationCheck


_DIAGNOSTIC_PATH_LIMIT = 100
_DIAGNOSTIC_PATH_LENGTH = 240


def _non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


def _paths(values: tuple[str, ...], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    for value in values:
        _non_empty(value, field_name)
        if value.startswith("/") or value == ".." or value.startswith("../"):
            raise ValueError(f"{field_name} must contain repository-relative paths")


class GitDeliveryMode(StrEnum):
    COMMIT_REQUIRED = "commit_required"
    NO_COMMIT_REQUIRED = "no_commit_required"


class GitDeliveryFailureCode(StrEnum):
    """Stable, non-secret classification for a failed Git delivery boundary."""

    BASELINE_MISMATCH = "baseline_mismatch"
    UNEXPECTED_HEAD_CHANGE = "unexpected_head_change"
    UNRELATED_WORKTREE_CHANGES = "unrelated_worktree_changes"
    UNRELATED_STAGED_CHANGES = "unrelated_staged_changes"
    EXPECTED_PATHS_MISMATCH = "expected_paths_mismatch"
    STAGING_FAILED = "staging_failed"
    COMMIT_MISSING = "commit_missing"
    COMMIT_CREATION_FAILED = "commit_creation_failed"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"


class GitOwnershipStatus(StrEnum):
    NOT_VERIFIED = "not_verified"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"


def _diagnostic_paths(values: tuple[str, ...], field_name: str) -> None:
    if len(values) > _DIAGNOSTIC_PATH_LIMIT:
        raise ValueError(f"{field_name} exceeds diagnostic path count")
    _paths(values, field_name)
    if any(
        len(value) > _DIAGNOSTIC_PATH_LENGTH
        or any(ord(character) < 32 for character in value)
        for value in values
    ):
        raise ValueError(f"{field_name} exceeds diagnostic path bounds")


@dataclass(frozen=True)
class GitDeliveryFailureDetails:
    """Bounded evidence safe to persist without raw Git output."""

    failure_code: GitDeliveryFailureCode
    baseline_head: str | None
    current_head: str | None
    expected_paths: tuple[str, ...]
    actual_paths: tuple[str, ...]
    staged_paths: tuple[str, ...]
    task_commit: str | None
    ownership_status: GitOwnershipStatus
    retry_safe: bool
    safe_summary: str

    def __post_init__(self) -> None:
        for name, value in (
            ("baseline_head", self.baseline_head),
            ("current_head", self.current_head),
            ("task_commit", self.task_commit),
        ):
            if value is not None and (not value or len(value) > 128):
                raise ValueError(f"{name} exceeds diagnostic bounds")
        for values, name in (
            (self.expected_paths, "expected_paths"),
            (self.actual_paths, "actual_paths"),
            (self.staged_paths, "staged_paths"),
        ):
            _diagnostic_paths(values, name)
        if type(self.retry_safe) is not bool:
            raise ValueError("retry_safe must be boolean")
        if not 1 <= len(self.safe_summary) <= 200 or any(
            ord(character) < 32 for character in self.safe_summary
        ):
            raise ValueError("safe_summary exceeds diagnostic bounds")


@dataclass(frozen=True)
class GitBaseline:
    task_id: str
    repository_root: str
    baseline_head: str
    status_entries: tuple[str, ...]

    def __post_init__(self) -> None:
        _non_empty(self.task_id, "task_id")
        _non_empty(self.repository_root, "repository_root")
        _non_empty(self.baseline_head, "baseline_head")


@dataclass(frozen=True)
class GitChangeSet:
    task_id: str
    repository_root: str
    baseline_head: str
    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    staged_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _non_empty(self.task_id, "task_id")
        _non_empty(self.repository_root, "repository_root")
        _non_empty(self.baseline_head, "baseline_head")
        _paths(self.changed_paths, "changed_paths")
        _paths(self.untracked_paths, "untracked_paths")
        _paths(self.staged_paths, "staged_paths")
        changed = set(self.changed_paths)
        if not set(self.untracked_paths).issubset(changed):
            raise ValueError("untracked_paths must be included in changed_paths")
        if not set(self.staged_paths).issubset(changed):
            raise ValueError("staged_paths must be included in changed_paths")

    @property
    def delivery_mode(self) -> GitDeliveryMode:
        return (
            GitDeliveryMode.COMMIT_REQUIRED
            if self.changed_paths
            else GitDeliveryMode.NO_COMMIT_REQUIRED
        )


@dataclass(frozen=True)
class GitNoCommitResult:
    task_id: str
    repository_root: str
    baseline_head: str
    verified_head: str
    verified_at: datetime
    delivery_mode: GitDeliveryMode = GitDeliveryMode.NO_COMMIT_REQUIRED

    def __post_init__(self) -> None:
        _non_empty(self.task_id, "task_id")
        _non_empty(self.repository_root, "repository_root")
        _non_empty(self.baseline_head, "baseline_head")
        _non_empty(self.verified_head, "verified_head")
        if self.delivery_mode is not GitDeliveryMode.NO_COMMIT_REQUIRED:
            raise ValueError("GitNoCommitResult must not require a commit")
        if self.verified_head != self.baseline_head:
            raise ValueError("no-commit delivery must preserve HEAD")


@dataclass(frozen=True)
class NoChangeDeliveryRecoveryEvidence:
    task_id: str
    attempt: int
    baseline_head: str
    current_head: str | None
    reported_paths: tuple[str, ...]
    actual_unstaged_paths: tuple[str, ...]
    actual_staged_paths: tuple[str, ...]
    report_persisted: bool
    supervisor_reviewed: bool
    commit_created: bool
    delivery_mode: GitDeliveryMode
    continuation_safe: bool

    def __post_init__(self) -> None:
        _non_empty(self.task_id, "task_id")
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        _non_empty(self.baseline_head, "baseline_head")
        for paths, name in (
            (self.reported_paths, "reported_paths"),
            (self.actual_unstaged_paths, "actual_unstaged_paths"),
            (self.actual_staged_paths, "actual_staged_paths"),
        ):
            _paths(paths, name)
        if self.current_head == "":
            raise ValueError("current_head must not be empty")


@dataclass(frozen=True)
class GitCommitResult:
    task_id: str
    repository_root: str
    baseline_head: str
    commit_sha: str
    commit_message: str
    changed_paths: tuple[str, ...]
    staged_paths: tuple[str, ...]
    committed_at: datetime

    def __post_init__(self) -> None:
        _non_empty(self.task_id, "task_id")
        _non_empty(self.repository_root, "repository_root")
        _non_empty(self.baseline_head, "baseline_head")
        _non_empty(self.commit_sha, "commit_sha")
        _non_empty(self.commit_message, "commit_message")
        _paths(self.changed_paths, "changed_paths")
        _paths(self.staged_paths, "staged_paths")
        if not self.changed_paths:
            raise ValueError("changed_paths must not be empty")
        if self.staged_paths != self.changed_paths:
            raise ValueError("staged_paths must exactly match changed_paths")


class GitDeliveryError(RuntimeError):
    """Base class for fail-closed Git delivery failures."""

    def __init__(
        self,
        message: str,
        *,
        details: GitDeliveryFailureDetails | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details


class DirtyGitBaseline(GitDeliveryError):
    """The repository contained changes before Task dispatch."""


class GitOwnershipError(GitDeliveryError):
    """The Task cannot exclusively own the observed repository changes."""


class WorkerVerificationError(GitDeliveryError):
    """Worker verification evidence does not meet delivery prerequisites."""

    def __init__(
        self,
        message: str,
        check: WorkerVerificationCheck | None = None,
        *,
        unmet_checks: tuple[WorkerVerificationCheck, ...] = (),
    ):
        super().__init__(message)
        self.check = check
        # Every check that blocked delivery, not only the first one.
        self.unmet_checks = unmet_checks


class UnexpectedGitHead(GitDeliveryError):
    """HEAD changed outside the delivery transaction."""


class EmptyGitChangeSet(GitDeliveryError):
    """An accepted implementation Task produced no repository change."""


class GitStagingError(GitDeliveryError):
    """Precise staging failed or staged unexpected paths."""


class GitCommitError(GitDeliveryError):
    """The local Task commit could not be created or verified."""


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
    "GitDeliveryError",
    "GitOwnershipError",
    "GitOwnershipStatus",
    "GitStagingError",
    "GitNoCommitResult",
    "NoChangeDeliveryRecoveryEvidence",
    "UnexpectedGitHead",
    "WorkerVerificationError",
]
