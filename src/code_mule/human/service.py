"""Deterministic, action-scoped human resolution operations."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import (
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import HumanAction, HumanResolution, ProjectEvent
from code_mule.domain.state_machine import validate_transition
from code_mule.state.models import ProjectState


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...
    def save(self, state: ProjectState) -> None: ...


class HumanResolutionError(RuntimeError):
    """Base class for fail-closed human resolution errors."""


class HumanActionNotFound(HumanResolutionError):
    """Raised when an action ID is not the unique pending action."""


class InvalidHumanResolution(HumanResolutionError):
    """Raised when an action cannot accept the requested operation."""


_APPROVABLE = frozenset(
    {
        HumanActionCategory.WORKER_APPROVAL,
        HumanActionCategory.EXTERNAL_SIDE_EFFECT,
    }
)
_RETRYABLE = frozenset(
    {
        HumanActionCategory.ATTEMPT_LIMIT,
        HumanActionCategory.DEPENDENCY_BLOCK,
    }
)


class HumanResolutionService:
    """Close exactly one pending action and persist its audit boundary."""

    def __init__(
        self,
        store: ProjectStateStore,
        *,
        clock: Callable[[], datetime],
        event_id_factory: Callable[[], str],
        resolution_id_factory: Callable[[], str],
    ) -> None:
        self._store = store
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._resolution_id_factory = resolution_id_factory

    def approve(self, action_id: str) -> ProjectState:
        state, action = self._target(action_id)
        if action.category not in _APPROVABLE:
            raise InvalidHumanResolution(
                f"{action.category.value} must be handled with resolve"
            )
        return self._close_action(
            state,
            action,
            HumanActionStatus.APPROVED,
            "human_action.approved",
            HumanResolutionStrategy.APPROVE,
        )

    def reject(self, action_id: str) -> ProjectState:
        state, action = self._target(action_id)
        return self._close_action(
            state,
            action,
            HumanActionStatus.REJECTED,
            "human_action.rejected",
            HumanResolutionStrategy.REJECT,
        )

    def resolve(
        self, action_id: str, strategy: HumanResolutionStrategy
    ) -> ProjectState:
        state, action = self._target(action_id)
        if action.category in _APPROVABLE:
            raise InvalidHumanResolution(
                f"{action.category.value} must be approved or rejected"
            )
        operation_time = self._clock()
        project = state.project
        tasks = state.tasks
        if strategy is HumanResolutionStrategy.RETRY_TASK:
            if action.category not in _RETRYABLE or action.task_id is None:
                raise InvalidHumanResolution(
                    "retry_task is not safe for this HumanAction"
                )
            matches = tuple(task for task in tasks if task.id == action.task_id)
            if len(matches) != 1:
                raise InvalidHumanResolution("HumanAction task is unavailable")
            task = matches[0]
            if task.status not in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}:
                raise InvalidHumanResolution("retry_task requires a stopped Task")
            retried = replace(
                task, status=TaskStatus.REOPENED, updated_at=operation_time
            )
            tasks = tuple(retried if item.id == retried.id else item for item in tasks)
            validate_transition(project.status, ProjectStatus.RUNNING)
            project = replace(
                project,
                status=ProjectStatus.RUNNING,
                current_task_id=None,
                updated_at=operation_time,
            )
            summary = "Boss explicitly reopened the stopped Task"
        elif strategy is HumanResolutionStrategy.FAIL_PROJECT:
            validate_transition(project.status, ProjectStatus.FAILED)
            project = replace(
                project,
                status=ProjectStatus.FAILED,
                current_task_id=None,
                updated_at=operation_time,
            )
            summary = "Boss explicitly stopped the project"
        elif strategy is HumanResolutionStrategy.ACKNOWLEDGE:
            summary = "Boss acknowledged the action; project remains safely stopped"
        else:
            raise InvalidHumanResolution("unsupported resolution strategy")

        resolution = HumanResolution(
            id=self._resolution_id_factory(),
            action_id=action.id,
            project_id=state.project.id,
            strategy=strategy,
            summary=summary,
            created_at=operation_time,
        )
        closed = replace(
            action,
            status=HumanActionStatus.RESOLVED,
            resolved_at=operation_time,
        )
        event = self._event(
            state,
            action,
            "human_action.resolved",
            operation_time,
            {"strategy": strategy.value, "resolution_id": resolution.id},
        )
        updated = replace(
            state,
            project=project,
            tasks=tasks,
            human_actions=self._replace_action(state.human_actions, closed),
            human_resolutions=state.human_resolutions + (resolution,),
            events=state.events + (event,),
        )
        self._store.save(updated)
        return updated

    def _target(self, action_id: str) -> tuple[ProjectState, HumanAction]:
        state = self._store.load()
        if state.project.status is not ProjectStatus.HUMAN_REQUIRED:
            raise InvalidHumanResolution("project is not HUMAN_REQUIRED")
        matches = tuple(action for action in state.human_actions if action.id == action_id)
        if len(matches) != 1:
            raise HumanActionNotFound("HumanAction is unknown")
        action = matches[0]
        if action.status is not HumanActionStatus.PENDING:
            raise InvalidHumanResolution("HumanAction is already closed")
        pending = tuple(
            item for item in state.human_actions if item.status is HumanActionStatus.PENDING
        )
        if pending != (action,):
            raise InvalidHumanResolution("project must have one unique pending HumanAction")
        return state, action

    def _close_action(
        self,
        state: ProjectState,
        action: HumanAction,
        status: HumanActionStatus,
        event_type: str,
        strategy: HumanResolutionStrategy,
    ) -> ProjectState:
        operation_time = self._clock()
        closed = replace(action, status=status, resolved_at=operation_time)
        resolution = HumanResolution(
            id=self._resolution_id_factory(),
            action_id=action.id,
            project_id=state.project.id,
            strategy=strategy,
            summary=(
                "Boss approved the exact pending action"
                if strategy is HumanResolutionStrategy.APPROVE
                else "Boss rejected the exact pending action"
            ),
            created_at=operation_time,
        )
        event = self._event(
            state,
            action,
            event_type,
            operation_time,
            {"resolution_id": resolution.id},
        )
        updated = replace(
            state,
            project=replace(state.project, updated_at=operation_time),
            human_actions=self._replace_action(state.human_actions, closed),
            human_resolutions=state.human_resolutions + (resolution,),
            events=state.events + (event,),
        )
        self._store.save(updated)
        return updated

    def _event(
        self,
        state: ProjectState,
        action: HumanAction,
        event_type: str,
        timestamp: datetime,
        metadata: dict[str, str],
    ) -> ProjectEvent:
        return ProjectEvent(
            id=self._event_id_factory(),
            project_id=state.project.id,
            event_type=event_type,
            entity_id=action.id,
            timestamp=timestamp,
            metadata={
                "action_id": action.id,
                "category": action.category.value,
                **metadata,
            },
        )

    @staticmethod
    def _replace_action(
        actions: tuple[HumanAction, ...], updated: HumanAction
    ) -> tuple[HumanAction, ...]:
        return tuple(updated if action.id == updated.id else action for action in actions)


__all__ = [
    "HumanActionNotFound",
    "HumanResolutionError",
    "HumanResolutionService",
    "InvalidHumanResolution",
    "ProjectStateStore",
]
