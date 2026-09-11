"""Deterministic safety proof for post-completion replanning recovery."""

from dataclasses import dataclass
from pathlib import Path
import subprocess

from code_mule.domain.enums import (
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    PlanStatus,
    ProjectStatus,
    RevisionCheckStatus,
    RevisionStatus,
)
from code_mule.domain.models import ChangeRequest, HumanAction, Plan, ProjectEvent
from code_mule.execution.contracts import ExecutionLeaseStatus
from code_mule.recovery.contracts import (
    ExecutionPhase,
    ExecutionStopReason,
    WorkerTerminalState,
)
from code_mule.state.models import ProjectState

from .errors import PostCompletionReplanningStage, ReplanFailureCode


@dataclass(frozen=True)
class PostCompletionReplanningEvidence:
    """Bounded persisted facts for one failed revision replan."""

    action: HumanAction
    change_request: ChangeRequest
    base_plan: Plan
    base_revision: int
    requested_revision: int
    target_plan_version: int
    plan_materialized: bool
    revision_materialized: bool
    worker_started: bool
    failure_category: str
    failure_code: str | None
    failure_stage: str
    field_path: str | None
    failure_event: ProjectEvent


@dataclass(frozen=True)
class ReplanningRetrySafety:
    safe: bool
    reason: str
    evidence: PostCompletionReplanningEvidence | None


_SAFE_FIELD_PATHS = frozenset(
    {
        "change_request_id",
        "new_entity_ids",
        "tasks_to_reopen/tasks_to_cancel",
        "milestone_ids_reused",
        "affected_in_progress_tasks/affected_pending_tasks",
        "affected_task_ids",
        "affected_completed_tasks",
        "tasks_to_add",
        "milestones[*].task_ids",
        "tasks_to_add[*].supersedes_task_id",
        "tasks_to_add[*].derived_from_task_ids",
        "tasks_to_add[*].dependencies",
        "replacement_plan",
    }
)


def post_completion_replanning_failure_evidence(
    state: ProjectState, action: HumanAction
) -> PostCompletionReplanningEvidence | None:
    """Return exact persisted failure evidence, including legacy schema-v14 data."""

    if (
        action.category is not HumanActionCategory.SUPERVISOR_FAILURE
        or action.task_id is not None
    ):
        return None
    boundary = state.latest_execution_stop
    if (
        boundary is None
        or boundary.reason is not ExecutionStopReason.SUPERVISOR_FAILED
        or boundary.phase not in {
            ExecutionPhase.REPLANNING,
            ExecutionPhase.PROJECT,
        }
        or boundary.recorded_at != action.created_at
        or boundary.worker_started
        or boundary.worker_terminal_state is not WorkerTerminalState.NOT_STARTED
    ):
        return None
    events = tuple(
        event
        for event in state.events
        if event.event_type == "replanning.failed"
        and event.entity_id == state.project.id
        and event.timestamp == action.created_at
    )
    if len(events) != 1:
        return None
    event = events[0]
    operation = event.metadata.get("operation")
    if operation not in {None, "post_completion_replanning"}:
        return None
    change_id = event.metadata.get("change_request_id")
    candidates = tuple(
        change
        for change in state.change_requests
        if change.base_revision is not None
        and change.requested_revision is not None
        and change.base_plan_id is not None
        and change.base_plan_version is not None
        and change.created_at <= action.created_at
        and (change_id is None or change.id == change_id)
        and change.status in {
            ChangeRequestStatus.REJECTED,
            ChangeRequestStatus.PENDING,
        }
    )
    if len(candidates) != 1:
        return None
    change = candidates[0]
    base_plans = tuple(
        plan
        for plan in state.plans
        if plan.id == change.base_plan_id
        and plan.version == change.base_plan_version
    )
    base_revisions = tuple(
        revision
        for revision in state.revisions
        if revision.revision_number == change.base_revision
        and revision.plan_id == change.base_plan_id
        and revision.plan_version == change.base_plan_version
    )
    if len(base_plans) != 1 or len(base_revisions) != 1:
        return None
    target_version = change.base_plan_version + 1
    target_plans = tuple(
        plan
        for plan in state.plans
        if plan.change_request_id == change.id
        or plan.revision_number == change.requested_revision
        or (
            plan.base_plan_id == change.base_plan_id
            and plan.version == target_version
        )
    )
    target_revisions = tuple(
        revision
        for revision in state.revisions
        if revision.change_request_id == change.id
        or revision.revision_number == change.requested_revision
    )
    raw_code = event.metadata.get("validation_code")
    failure_code = (
        raw_code
        if raw_code in {item.value for item in ReplanFailureCode}
        else None
    )
    raw_stage = event.metadata.get("stage")
    failure_stage = (
        raw_stage
        if raw_stage in {item.value for item in PostCompletionReplanningStage}
        else PostCompletionReplanningStage.MATERIALIZATION.value
    )
    field_path = event.metadata.get("field_path")
    if field_path not in _SAFE_FIELD_PATHS:
        field_path = None
    category = event.metadata.get("failure_category")
    if category is None and event.metadata.get("error_type") == "ReplanMaterializationError":
        category = "deterministic_validation_failure"
    return PostCompletionReplanningEvidence(
        action=action,
        change_request=change,
        base_plan=base_plans[0],
        base_revision=change.base_revision,
        requested_revision=change.requested_revision,
        target_plan_version=target_version,
        plan_materialized=bool(target_plans),
        revision_materialized=bool(target_revisions),
        worker_started=boundary.worker_started,
        failure_category=category or "unknown_failure",
        failure_code=failure_code,
        failure_stage=failure_stage,
        field_path=field_path,
        failure_event=event,
    )


def post_completion_replanning_failure_is_persisted(
    state: ProjectState, action: HumanAction
) -> bool:
    evidence = post_completion_replanning_failure_evidence(state, action)
    return (
        evidence is not None
        and action.status is HumanActionStatus.PENDING
        and state.project.status is ProjectStatus.HUMAN_REQUIRED
        and evidence.change_request.status is ChangeRequestStatus.REJECTED
    )


def post_completion_replanning_retry_safety(
    state: ProjectState,
    action: HumanAction,
    *,
    validate_workspace: bool = True,
    owned_lease_id: str | None = None,
) -> ReplanningRetrySafety:
    evidence = post_completion_replanning_failure_evidence(state, action)
    if not post_completion_replanning_failure_is_persisted(state, action):
        return ReplanningRetrySafety(False, "failure evidence does not match", evidence)
    return _retry_safety(
        state,
        evidence,
        validate_workspace=validate_workspace,
        owned_lease_id=owned_lease_id,
    )


def post_completion_replanning_recovery_safety(
    state: ProjectState,
    *,
    validate_workspace: bool = True,
    owned_lease_id: str | None = None,
) -> ReplanningRetrySafety:
    resolutions = tuple(
        resolution
        for resolution in state.human_resolutions
        if resolution.strategy is HumanResolutionStrategy.RETRY_REPLANNING
    )
    if not resolutions:
        return ReplanningRetrySafety(False, "retry authorization is absent", None)
    resolution = resolutions[-1]
    actions = tuple(
        action
        for action in state.human_actions
        if action.id == resolution.action_id
        and action.status is HumanActionStatus.RESOLVED
        and action.resolved_at == resolution.created_at
    )
    if len(actions) != 1:
        return ReplanningRetrySafety(False, "resolved action evidence is ambiguous", None)
    evidence = post_completion_replanning_failure_evidence(state, actions[0])
    if (
        evidence is None
        or state.project.status is not ProjectStatus.CHANGE_REQUESTED
        or evidence.change_request.status is not ChangeRequestStatus.PENDING
        or any(
            action.status is HumanActionStatus.PENDING
            for action in state.human_actions
        )
    ):
        return ReplanningRetrySafety(False, "replanning-ready state is inconsistent", evidence)
    return _retry_safety(
        state,
        evidence,
        validate_workspace=validate_workspace,
        owned_lease_id=owned_lease_id,
    )


def _retry_safety(
    state: ProjectState,
    evidence: PostCompletionReplanningEvidence,
    *,
    validate_workspace: bool,
    owned_lease_id: str | None,
) -> ReplanningRetrySafety:
    change = evidence.change_request
    revision = next(
        item
        for item in state.revisions
        if item.revision_number == evidence.base_revision
    )
    if evidence.plan_materialized or evidence.revision_materialized:
        return ReplanningRetrySafety(False, "target Plan or Revision already exists", evidence)
    if (
        state.project.active_plan_id != evidence.base_plan.id
        or state.project.current_task_id is not None
        or evidence.base_plan.status is not PlanStatus.COMPLETED
        or revision.lifecycle_status is not RevisionStatus.COMPLETED
        or revision.completion_head is None
        or revision.verification_status is not RevisionCheckStatus.PASS
        or revision.final_review_status is not RevisionCheckStatus.PASS
    ):
        return ReplanningRetrySafety(False, "completed base revision is not immutable and trusted", evidence)
    if state.impact_analyses and any(
        impact.change_request_id == change.id for impact in state.impact_analyses
    ):
        return ReplanningRetrySafety(False, "impact result was already persisted", evidence)
    if any(req.introduced_by == change.id for req in state.requirements):
        return ReplanningRetrySafety(False, "new Requirement evidence already exists", evidence)
    plan_ids = {plan.id for plan in state.plans}
    milestone_ids = {
        milestone.id
        for milestone in state.milestones
        if milestone.plan_id in plan_ids
    }
    task_ids = {
        task_id
        for milestone in state.milestones
        if milestone.id in milestone_ids
        for task_id in milestone.task_ids
    }
    if len(milestone_ids) != len(state.milestones) or any(
        task.id not in task_ids for task in state.tasks
    ):
        return ReplanningRetrySafety(False, "orphan materialization evidence exists", evidence)
    if any(
        attempt.started_at >= change.created_at
        for attempt in state.execution_attempts
    ) or any(
        report.created_at >= change.created_at
        for report in state.execution_reports
    ) or any(
        result.committed_at >= change.created_at
        for result in state.git_commit_results
    ):
        return ReplanningRetrySafety(False, "Worker or Git delivery evidence exists", evidence)
    active_lease_ids = tuple(
        lease.id
        for lease in state.execution_leases
        if lease.status is ExecutionLeaseStatus.ACTIVE
    )
    if active_lease_ids and active_lease_ids != (owned_lease_id,):
        return ReplanningRetrySafety(False, "an execution lease is active", evidence)
    if validate_workspace:
        reason = _workspace_failure(state, revision.completion_head)
        if reason is not None:
            return ReplanningRetrySafety(False, reason, evidence)
    return ReplanningRetrySafety(True, "fresh replanning is proven side-effect free", evidence)


def _workspace_failure(state: ProjectState, expected_head: str) -> str | None:
    workspace = state.project.workspace
    if workspace is None:
        return "workspace is unavailable"
    root = Path(workspace)
    if not root.is_absolute() or not root.is_dir():
        return "workspace is unavailable"
    try:
        repository = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        status = subprocess.run(
            ("git", "status", "--short", "--untracked-files=all"),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Git workspace validation failed"
    if (
        repository.returncode != 0
        or Path(repository.stdout.strip()).resolve() != root.resolve()
    ):
        return "workspace is not the recorded repository root"
    if head.returncode != 0 or head.stdout.strip() != expected_head:
        return "workspace HEAD differs from Revision completion"
    if status.returncode != 0 or status.stdout.strip():
        return "workspace is not clean"
    return None


__all__ = [
    "PostCompletionReplanningEvidence",
    "ReplanningRetrySafety",
    "post_completion_replanning_failure_evidence",
    "post_completion_replanning_failure_is_persisted",
    "post_completion_replanning_recovery_safety",
    "post_completion_replanning_retry_safety",
]
