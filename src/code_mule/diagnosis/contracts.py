"""Typed, bounded facts for deterministic project diagnosis."""

from dataclasses import dataclass
from enum import StrEnum

from code_mule.domain import ProjectStatus
from code_mule.domain.worker_verification import WorkerCheckStatus, WorkerCheckType


class DiagnosisBlockerCategory(StrEnum):
    NONE = "none"
    WORKER_INPUT = "worker_input"
    WORKER_APPROVAL = "worker_approval"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    WORKER_VERIFICATION = "worker_verification"
    WORKSPACE_BLOCK = "workspace_block"
    RECOVERY_UNCERTAIN = "recovery_uncertain"
    ATTEMPT_LIMIT = "attempt_limit"
    SUPERVISOR_FAILURE = "supervisor_failure"
    DEPENDENCY_BLOCK = "dependency_block"
    PAUSED = "paused"
    CHANGE_REQUESTED = "change_requested"
    CANCELLATION_REQUESTED = "cancellation_requested"
    FAILED = "failed"
    INCONSISTENT_STATE = "inconsistent_state"


class DiagnosisStage(StrEnum):
    NONE = "none"
    PLANNING = "planning"
    WORKER_INPUT = "worker_input"
    WORKER_APPROVAL = "worker_approval"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    WORKER_VERIFICATION = "worker_verification"
    WORKSPACE_BASELINE = "workspace_baseline"
    EXECUTION_RECOVERY = "execution_recovery"
    TASK_REVIEW = "task_review"
    DEPENDENCY_RESOLUTION = "dependency_resolution"
    BOSS_CONTROL = "boss_control"
    REPLANNING = "replanning"
    CANCELLATION = "cancellation"
    PROJECT_STATE = "project_state"


class DiagnosisRecoverability(StrEnum):
    RECOVERABLE = "recoverable"
    NOT_APPLICABLE = "not_applicable"
    UNCERTAIN = "uncertain"
    TERMINAL = "terminal"


class DiagnosisNextAction(StrEnum):
    NONE = "none"
    CHANGE = "code-mule change ..."
    INSPECT = "code-mule inspect"
    RESUME = "code-mule resume"
    APPLY_CHANGE = "code-mule change --apply"
    STATUS = "code-mule status"
    RECOVER = "code-mule recover"


@dataclass(frozen=True)
class VerificationDiagnosis:
    check_name: str
    check_type: WorkerCheckType
    check_status: WorkerCheckStatus
    required: bool
    stage: DiagnosisStage = DiagnosisStage.WORKER_VERIFICATION

    def __post_init__(self) -> None:
        if not 1 <= len(self.check_name) <= 120:
            raise ValueError("check_name must contain 1..120 characters")


@dataclass(frozen=True)
class ProjectDiagnosis:
    project_name: str
    project_status: ProjectStatus
    active_plan_version: int | None
    completed_tasks: int
    total_tasks: int
    current_task_id: str | None
    current_task_title: str | None
    latest_completed_task_id: str | None
    latest_completed_task_title: str | None
    latest_task_commit: str | None
    blocker_category: DiagnosisBlockerCategory
    blocker_stage: DiagnosisStage
    blocker_summary: str
    recoverability: DiagnosisRecoverability
    boss_action_required: bool
    recommended_next_action: DiagnosisNextAction
    pending_action_id: str | None = None
    worker_input_question: str | None = None
    verification: VerificationDiagnosis | None = None
    last_safe_point: str | None = None
    stop_reason: str | None = None
    recovery_mode: str | None = None
    recovery_command: str | None = None
    revision_number: int | None = None
    requested_revision: int | None = None
    base_revision: int | None = None
    base_plan_id: str | None = None
    base_plan_version: int | None = None
    change_summary: str | None = None

    def __post_init__(self) -> None:
        bounded = {
            "project_name": (self.project_name, 200),
            "blocker_summary": (self.blocker_summary, 300),
        }
        optional = {
            "current_task_id": (self.current_task_id, 128),
            "current_task_title": (self.current_task_title, 200),
            "latest_completed_task_id": (self.latest_completed_task_id, 128),
            "latest_completed_task_title": (self.latest_completed_task_title, 200),
            "latest_task_commit": (self.latest_task_commit, 128),
            "pending_action_id": (self.pending_action_id, 128),
            "worker_input_question": (self.worker_input_question, 2_000),
        }
        for name, (value, limit) in bounded.items():
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValueError(f"{name} exceeds diagnosis bounds")
        for name, (value, limit) in optional.items():
            if value is not None and (not value or len(value) > limit):
                raise ValueError(f"{name} exceeds diagnosis bounds")
        if self.active_plan_version is not None and self.active_plan_version < 1:
            raise ValueError("active_plan_version must be positive")
        if not 0 <= self.completed_tasks <= self.total_tasks:
            raise ValueError("task progress is invalid")


__all__ = [
    "DiagnosisBlockerCategory",
    "DiagnosisNextAction",
    "DiagnosisRecoverability",
    "DiagnosisStage",
    "ProjectDiagnosis",
    "VerificationDiagnosis",
]
