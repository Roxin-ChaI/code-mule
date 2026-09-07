"""Enumerations used by the Code Mule domain contracts."""

from enum import StrEnum


class ProjectStatus(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    RUNNING = "running"
    CHANGE_REQUESTED = "change_requested"
    REPLANNING = "replanning"
    CANCEL_REQUESTED = "cancel_requested"
    PAUSED_BY_BOSS = "paused_by_boss"
    HUMAN_REQUIRED = "human_required"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    STOP = "stop"


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


class RevisionStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class RevisionCheckStatus(StrEnum):
    NOT_RUN = "not_run"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class HumanActionCategory(StrEnum):
    WORKER_APPROVAL = "worker_approval"
    WORKER_INPUT = "worker_input"
    ATTEMPT_LIMIT = "attempt_limit"
    SUPERVISOR_FAILURE = "supervisor_failure"
    DEPENDENCY_BLOCK = "dependency_block"
    WORKSPACE_BLOCK = "workspace_block"
    WORKER_VERIFICATION = "worker_verification"
    RECOVERY_UNCERTAIN = "recovery_uncertain"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    UNKNOWN = "unknown"


class WorkerHumanActionKind(StrEnum):
    INPUT = "input"
    APPROVAL = "approval"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


class HumanActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class HumanResolutionStrategy(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    RETRY_TASK = "retry_task"
    FAIL_PROJECT = "fail_project"
    ACKNOWLEDGE = "acknowledge"
    ANSWER = "answer"


__all__ = [
    "BossCommandType",
    "ChangeRequestStatus",
    "HumanActionCategory",
    "HumanActionStatus",
    "HumanResolutionStrategy",
    "WorkerHumanActionKind",
    "PlanStatus",
    "ProjectStatus",
    "RequirementStatus",
    "RevisionCheckStatus",
    "RevisionStatus",
    "SupervisorDecisionType",
    "TaskStatus",
]
