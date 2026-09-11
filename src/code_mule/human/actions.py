"""Shared creation logic for durable, action-scoped human gates."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from code_mule.domain.enums import (
    HumanActionCategory,
    HumanActionStatus,
    ProjectStatus,
)
from code_mule.domain.models import (
    HumanAction,
    ProjectEvent,
    WorkerCapabilityApprovalDetails,
    WorkerInputDetails,
)
from code_mule.domain.state_machine import validate_transition
from code_mule.recovery.contracts import (
    BoundaryRecoverability,
    ExecutionPhase,
    ExecutionStopReason,
    SafePointKind,
    WorkerTerminalState,
)
from code_mule.recovery.state import with_safe_point, with_stop_boundary
from code_mule.state.models import ProjectState


def pending_action(state: ProjectState) -> HumanAction | None:
    """Return the sole pending action, failing closed on ambiguous state."""

    actions = tuple(
        action
        for action in state.human_actions
        if action.status is HumanActionStatus.PENDING
    )
    if len(actions) > 1:
        raise ValueError("project has multiple pending HumanActions")
    return actions[0] if actions else None


def request_human_action(
    state: ProjectState,
    *,
    category: HumanActionCategory,
    summary: str,
    requested_action: str,
    risk: str,
    task_id: str | None,
    operation_time: datetime,
    action_id: str,
    event_id_factory: Callable[[], str],
    source_event_types: tuple[str, ...],
    source_metadata: dict[str, str] | None = None,
    worker_input: WorkerInputDetails | None = None,
    capability_approval: WorkerCapabilityApprovalDetails | None = None,
    phase: ExecutionPhase | None = None,
) -> ProjectState:
    """Persist a single typed action and enter HUMAN_REQUIRED atomically."""

    if pending_action(state) is not None:
        raise ValueError("project already has a pending HumanAction")
    if any(action.id == action_id for action in state.human_actions):
        raise ValueError("HumanAction ID already exists")
    if state.project.status is not ProjectStatus.HUMAN_REQUIRED:
        validate_transition(state.project.status, ProjectStatus.HUMAN_REQUIRED)
    action = HumanAction(
        id=action_id,
        project_id=state.project.id,
        task_id=task_id,
        category=category,
        summary=summary,
        requested_action=requested_action,
        risk=risk,
        status=HumanActionStatus.PENDING,
        created_at=operation_time,
        worker_input=worker_input,
        capability_approval=capability_approval,
    )
    source_events = tuple(
        ProjectEvent(
            id=event_id_factory(),
            project_id=state.project.id,
            event_type=event_type,
            entity_id=task_id or state.project.id,
            timestamp=operation_time,
            metadata=dict(source_metadata or {}),
        )
        for event_type in source_event_types
    )
    requested = ProjectEvent(
        id=event_id_factory(),
        project_id=state.project.id,
        event_type="human_action.requested",
        entity_id=action.id,
        timestamp=operation_time,
        metadata={
            "action_id": action.id,
            "category": category.value,
            "status": action.status.value,
        },
    )
    updated = replace(
        state,
        project=replace(
            state.project,
            status=ProjectStatus.HUMAN_REQUIRED,
            updated_at=operation_time,
        ),
        human_actions=state.human_actions + (action,),
        events=state.events + (requested,) + source_events,
    )
    attempt = None
    worker_started = False
    worker_state = WorkerTerminalState.NOT_STARTED
    report_persisted = False
    head_sha = None
    if task_id is not None:
        attempts = tuple(item for item in state.execution_attempts if item.task_id == task_id)
        if attempts:
            latest = max(attempts, key=lambda item: item.attempt)
            attempt = latest.attempt
            worker_started = latest.status.value != "prepared"
            report_persisted = latest.status.value in {
                "report_persisted", "review_started", "review_completed",
                "delivery_started", "delivered",
            }
            head_sha = latest.baseline_head
            worker_state = (
                WorkerTerminalState.COMPLETED
                if report_persisted or latest.status.value == "worker_completed"
                else WorkerTerminalState.UNKNOWN
                if worker_started
                else WorkerTerminalState.NOT_STARTED
            )
    reason = {
        HumanActionCategory.WORKER_INPUT: ExecutionStopReason.WORKER_INPUT,
        HumanActionCategory.WORKER_VERIFICATION: ExecutionStopReason.WORKER_VERIFICATION,
        HumanActionCategory.WORKSPACE_BLOCK: ExecutionStopReason.WORKSPACE_BLOCK,
        HumanActionCategory.ATTEMPT_LIMIT: ExecutionStopReason.ATTEMPT_LIMIT,
        HumanActionCategory.SUPERVISOR_FAILURE: ExecutionStopReason.SUPERVISOR_FAILED,
        HumanActionCategory.RECOVERY_UNCERTAIN: ExecutionStopReason.RECOVERY_UNCERTAIN,
    }.get(category, ExecutionStopReason.HUMAN_REQUIRED)
    updated = with_safe_point(
        updated,
        SafePointKind.HUMAN_GATE,
        operation_time,
        task_id=task_id,
        attempt=attempt,
        head_sha=head_sha,
    )
    return with_stop_boundary(
        updated,
        reason=reason,
        phase=(phase or (ExecutionPhase.WORKER if task_id else ExecutionPhase.PROJECT)),
        safe_point=SafePointKind.HUMAN_GATE,
        recoverability=(
            BoundaryRecoverability.UNCERTAIN
            if category is HumanActionCategory.RECOVERY_UNCERTAIN
            or capability_approval is not None
            else BoundaryRecoverability.RECOVERABLE
        ),
        worker_started=worker_started,
        worker_terminal_state=worker_state,
        report_persisted=report_persisted,
        recorded_at=operation_time,
        task_id=task_id,
        attempt=attempt,
        head_sha=head_sha,
    )
