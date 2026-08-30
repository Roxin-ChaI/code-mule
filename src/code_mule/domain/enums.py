"""Enumerations used by the Code Mule domain contracts."""

from enum import StrEnum


class ProjectStatus(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    RUNNING = "running"
    CHANGE_REQUESTED = "change_requested"
    REPLANNING = "replanning"
    PAUSED_BY_BOSS = "paused_by_boss"
    HUMAN_REQUIRED = "human_required"
    DONE = "done"
    FAILED = "failed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    REOPENED = "reopened"


class SupervisorDecisionType(StrEnum):
    CONTINUE = "continue"
    REWORK = "rework"
    HUMAN_REQUIRED = "human_required"
    DONE = "done"


class BossCommandType(StrEnum):
    QUERY = "query"
    CHANGE = "change"
    PAUSE = "pause"
    RESUME = "resume"


class RequirementStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REMOVED = "removed"


class PlanStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"


class ChangeRequestStatus(StrEnum):
    PENDING = "pending"
    ANALYZING = "analyzing"
    APPLIED = "applied"
    REJECTED = "rejected"


__all__ = [
    "BossCommandType",
    "ChangeRequestStatus",
    "PlanStatus",
    "ProjectStatus",
    "RequirementStatus",
    "SupervisorDecisionType",
    "TaskStatus",
]
