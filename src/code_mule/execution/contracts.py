"""Typed local execution ownership and recovery contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ExecutionLeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    STALE = "stale"


class RecoveryClassification(StrEnum):
    SAFE_TO_RESUME = "safe_to_resume"
    SESSION_RECOVERY_REQUIRED = "session_recovery_required"
    SIDE_EFFECT_UNCERTAIN = "side_effect_uncertain"
    STALE_IDLE_LEASE = "stale_idle_lease"


@dataclass(frozen=True)
class ExecutionLease:
    id: str
    project_id: str
    owner_id: str
    pid: int
    acquired_at: datetime
    heartbeat_at: datetime
    status: ExecutionLeaseStatus
    current_task_id: str | None = None
    codex_thread_id: str | None = None
    attempt: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("id", "project_id", "owner_id"):
            if getattr(self, field_name) == "":
                raise ValueError(f"{field_name} must not be empty")
        if self.pid < 1:
            raise ValueError("pid must be positive")
        if self.attempt is not None and self.attempt < 1:
            raise ValueError("attempt must be positive when present")
        if self.codex_thread_id == "":
            raise ValueError("codex_thread_id must not be empty")
        if self.codex_thread_id is not None and self.current_task_id is None:
            raise ValueError("Codex thread identity requires current_task_id")
        if self.attempt is not None and self.current_task_id is None:
            raise ValueError("attempt requires current_task_id")


@dataclass(frozen=True)
class RecoveryDecision:
    classification: RecoveryClassification
    previous_lease_id: str
    task_id: str | None

    def __post_init__(self) -> None:
        if self.previous_lease_id == "":
            raise ValueError("previous_lease_id must not be empty")


class ExecutionOwnershipError(RuntimeError):
    """Base class for local ownership failures."""


class ExecutionAlreadyOwned(ExecutionOwnershipError):
    def __init__(self, lease: ExecutionLease) -> None:
        super().__init__("project already has a live execution owner")
        self.lease = lease


class ExecutionRecoveryRequired(ExecutionOwnershipError):
    def __init__(self, decision: RecoveryDecision) -> None:
        super().__init__("previous execution requires human recovery")
        self.decision = decision


__all__ = [
    "ExecutionAlreadyOwned",
    "ExecutionLease",
    "ExecutionLeaseStatus",
    "ExecutionOwnershipError",
    "ExecutionRecoveryRequired",
    "RecoveryClassification",
    "RecoveryDecision",
]
