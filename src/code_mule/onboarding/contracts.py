"""Typed facts for persistent installation, doctor, and start preflight."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StartDecision(StrEnum):
    """Deterministic outcome of the read-only start preflight."""

    EXISTING_PROJECT = "existing-project"
    READY_TO_INIT = "ready-to-init"
    DIRTY_WORKSPACE = "dirty-workspace"
    NO_GIT_HEAD = "no-git-head"
    NOT_GIT_REPO = "not-git-repo"


@dataclass(frozen=True)
class DoctorCheck:
    """One row of the deterministic local environment doctor."""

    label: str
    status: str
    healthy: bool
    details: tuple[str, ...] = ()
    advice: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.label == "":
            raise ValueError("doctor check label must not be empty")
        if self.status == "":
            raise ValueError("doctor check status must not be empty")


@dataclass(frozen=True)
class DoctorReport:
    """Complete doctor result; contains no credentials or raw subprocess logs."""

    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return bool(self.checks) and all(check.healthy for check in self.checks)

    @property
    def problem_count(self) -> int:
        return sum(not check.healthy for check in self.checks)

    def check(self, label: str) -> DoctorCheck | None:
        return next(
            (check for check in self.checks if check.label == label),
            None,
        )


@dataclass(frozen=True)
class WorkspaceProbe:
    """Read-only Git facts for one workspace; never mutates the repository."""

    git_available: bool
    repository_root: Path | None = None
    head: str | None = None
    clean: bool | None = None
    status_entries: tuple[str, ...] = ()
    issue: str | None = None


@dataclass(frozen=True)
class StartPreflight:
    """Read-only result used by `code-mule start` before any side effects."""

    decision: StartDecision
    workspace: Path
    state_file: Path
    state_exists: bool = False
    probe: WorkspaceProbe | None = None
    heading: str = ""
    reason: str = ""
    next_commands: tuple[str, ...] = ()


__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "StartDecision",
    "StartPreflight",
    "WorkspaceProbe",
]
