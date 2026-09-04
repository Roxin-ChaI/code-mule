"""Shared creation logic for durable, action-scoped human gates."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from code_mule.domain.enums import (
    HumanActionCategory,
    HumanActionStatus,
    ProjectStatus,
)
from code_mule.domain.models import HumanAction, ProjectEvent, WorkerInputDetails
from code_mule.domain.state_machine import validate_transition
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
    return replace(
        state,
        project=replace(
            state.project,
            status=ProjectStatus.HUMAN_REQUIRED,
            updated_at=operation_time,
        ),
        human_actions=state.human_actions + (action,),
        events=state.events + (requested,) + source_events,
    )
