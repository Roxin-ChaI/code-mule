"""Typed, bounded facts for deterministic project diagnosis."""

from dataclasses import dataclass
from enum import StrEnum

from code_mule.domain import ProjectStatus
from code_mule.domain.worker_verification import WorkerCheckStatus, WorkerCheckType
from code_mule.recovery.contracts import WorkerUncertaintyEvidence
from code_mule.git_delivery.contracts import (
    GitDeliveryFailureDetails,
    NoChangeDeliveryRecoveryEvidence,
)


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
    FINAL_REVIEW_DECISION = "final_review_decision"
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
    WORKER_EXECUTION = "worker_execution"
    GIT_DELIVERY = "git_delivery"
    TASK_REVIEW = "task_review"
    DEPENDENCY_RESOLUTION = "dependency_resolution"
    BOSS_CONTROL = "boss_control"
    REPLANNING = "replanning"
    FINAL_REVIEW = "final_review"
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
    target_plan_version: int | None = None
    plan_materialized: bool | None = None
    requested_revision_materialized: bool | None = None
    failure_category: str | None = None
    failure_code: str | None = None
    failure_field_path: str | None = None
    failure_summary: str | None = None
    change_summary: str | None = None
    final_review_outcome: str | None = None
    project_verification_status: str | None = None
    completion_head_candidate: str | None = None
    worker_uncertainty: WorkerUncertaintyEvidence | None = None
    no_change_delivery: NoChangeDeliveryRecoveryEvidence | None = None
    git_delivery_failure: GitDeliveryFailureDetails | None = None
    worker_failure_class: str | None = None
    worker_transport_evidence_available: bool = False

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
            "failure_category": (self.failure_category, 64),
            "failure_code": (self.failure_code, 64),
            "failure_field_path": (self.failure_field_path, 160),
            "failure_summary": (self.failure_summary, 200),
            "final_review_outcome": (self.final_review_outcome, 64),
            "project_verification_status": (self.project_verification_status, 64),
            "completion_head_candidate": (self.completion_head_candidate, 128),
            "worker_failure_class": (self.worker_failure_class, 64),
        }
        for name, (value, limit) in bounded.items():
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValueError(f"{name} exceeds diagnosis bounds")
        for name, (value, limit) in optional.items():
            if value is not None and (not value or len(value) > limit):
                raise ValueError(f"{name} exceeds diagnosis bounds")
        if self.active_plan_version is not None and self.active_plan_version < 1:
            raise ValueError("active_plan_version must be positive")
        if type(self.worker_transport_evidence_available) is not bool:
            raise ValueError("worker_transport_evidence_available must be boolean")
        if self.target_plan_version is not None and self.target_plan_version < 1:
            raise ValueError("target_plan_version must be positive")
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
