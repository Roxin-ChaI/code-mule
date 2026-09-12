"""Typed project-level verification and final-review evidence."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProjectVerificationCategory(StrEnum):
    TEST = "test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    BUILD = "build"
    DELIVERY_MANIFEST = "delivery-manifest"
    GIT_CLEAN = "git-clean"


class ProjectVerificationStatus(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class FinalReviewDecision(StrEnum):
    APPROVE = "approve"
    HUMAN_REQUIRED = "human_required"


@dataclass(frozen=True)
class ProjectVerificationCommand:
    name: str
    category: ProjectVerificationCategory
    command: tuple[str, ...]
    required: bool = True
    timeout_seconds: float = 120.0
    network_allowed: bool = False

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("name must not be empty")
        if not self.command or any(part == "" for part in self.command):
            raise ValueError("command must contain non-empty arguments")
        if self.category in {
            ProjectVerificationCategory.GIT_CLEAN,
            ProjectVerificationCategory.DELIVERY_MANIFEST,
        }:
            raise ValueError("implicit trusted checks cannot be configured")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class ProjectVerificationSpec:
    project_id: str
    commands: tuple[ProjectVerificationCommand, ...]

    def __post_init__(self) -> None:
        if self.project_id == "":
            raise ValueError("project_id must not be empty")
        names = tuple(command.name for command in self.commands)
        if len(names) != len(set(names)):
            raise ValueError("verification command names must be unique")


@dataclass(frozen=True)
class ProjectVerificationCheck:
    name: str
    category: ProjectVerificationCategory
    command: tuple[str, ...]
    status: ProjectVerificationStatus
    exit_code: int | None
    safe_summary: str
    required: bool

    def __post_init__(self) -> None:
        if self.name == "" or self.safe_summary == "":
            raise ValueError("name and safe_summary must not be empty")
        if self.status in {
            ProjectVerificationStatus.PENDING,
            ProjectVerificationStatus.TIMEOUT,
        } and self.exit_code is not None:
            raise ValueError("pending/timeout checks cannot have an exit code")
        if self.status in {
            ProjectVerificationStatus.PASS,
            ProjectVerificationStatus.FAIL,
        } and self.exit_code is None:
            raise ValueError("completed checks require an exit code")


@dataclass(frozen=True)
class ProjectVerificationResult:
    id: str
    project_id: str
    plan_id: str
    expected_head: str
    verified_head: str
    checks: tuple[ProjectVerificationCheck, ...]
    started_at: datetime
    completed_at: datetime
    final_review_decision: FinalReviewDecision | None = None
    final_review_summary: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "project_id", "plan_id", "expected_head", "verified_head"):
            if getattr(self, name) == "":
                raise ValueError(f"{name} must not be empty")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.final_review_decision is None and self.final_review_summary is not None:
            raise ValueError("final review summary requires a decision")
        if self.final_review_decision is not None and not self.final_review_summary:
            raise ValueError("final review decision requires a summary")

    @property
    def passed(self) -> bool:
        return all(
            not check.required or check.status is ProjectVerificationStatus.PASS
            for check in self.checks
        )


class ProjectVerificationError(RuntimeError):
    """Base error for final project verification."""


class InvalidProjectVerificationState(ProjectVerificationError):
    """Persisted state cannot enter or resume final verification."""


__all__ = [
    "FinalReviewDecision",
    "InvalidProjectVerificationState",
    "ProjectVerificationCategory",
    "ProjectVerificationCheck",
    "ProjectVerificationCommand",
    "ProjectVerificationError",
    "ProjectVerificationResult",
    "ProjectVerificationSpec",
    "ProjectVerificationStatus",
]
