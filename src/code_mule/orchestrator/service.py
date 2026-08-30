"""Deterministic command handling over persisted project state."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import (
    ChangeRequestStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import ChangeRequest, ProjectEvent
from code_mule.domain.state_machine import validate_transition
from code_mule.state.models import ProjectState

from .commands import ChangeCommand, PauseCommand, QueryCommand, ResumeCommand
from .results import ChangeResult, CommandResult, ProjectStatusView


class ProjectIdentityMismatch(ValueError):
    """Raised when a command targets a different project."""


class InvalidBossCommand(ValueError):
    """Raised when a Boss command is not legal in the current project state."""


class DuplicateChangeRequest(ValueError):
    """Raised when a ChangeRequest ID already exists in project state."""


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...

    def save(self, state: ProjectState) -> None: ...


class OrchestratorService:
    """Apply deterministic Boss commands to the Project State source of truth."""

    def __init__(
        self,
        store: ProjectStateStore,
        *,
        clock: Callable[[], datetime],
        event_id_factory: Callable[[], str],
    ):
        self._store = store
        self._clock = clock
        self._event_id_factory = event_id_factory

    def query(self, command: QueryCommand) -> ProjectStatusView:
        state = self._store.load()
        self._validate_identity(command.project_id, state)
        tasks = state.tasks
        open_change_statuses = {
            ChangeRequestStatus.PENDING,
            ChangeRequestStatus.ANALYZING,
        }
        return ProjectStatusView(
            project_id=state.project.id,
            project_name=state.project.name,
            status=state.project.status,
            active_plan_id=state.project.active_plan_id,
            current_task_id=state.project.current_task_id,
            total_tasks=len(tasks),
            completed_tasks=sum(
                task.status is TaskStatus.COMPLETED for task in tasks
            ),
            in_progress_tasks=sum(
                task.status is TaskStatus.IN_PROGRESS for task in tasks
            ),
            pending_tasks=sum(task.status is TaskStatus.PENDING for task in tasks),
            blocked_tasks=sum(task.status is TaskStatus.BLOCKED for task in tasks),
            open_change_requests=sum(
                request.status in open_change_statuses
                for request in state.change_requests
            ),
            quality_status=state.quality_status,
        )

    def pause(self, command: PauseCommand) -> CommandResult:
        state = self._store.load()
        self._validate_identity(command.project_id, state)
        if state.project.status is not ProjectStatus.RUNNING:
            raise InvalidBossCommand(
                f"pause is not allowed from {state.project.status}"
            )
        return self._change_project_status(
            state,
            target=ProjectStatus.PAUSED_BY_BOSS,
            event_type="boss.pause",
            command_name="pause",
            message="Project paused by Boss",
        )

    def resume(self, command: ResumeCommand) -> CommandResult:
        state = self._store.load()
        self._validate_identity(command.project_id, state)
        if state.project.status is not ProjectStatus.PAUSED_BY_BOSS:
            raise InvalidBossCommand(
                f"resume is not allowed from {state.project.status}"
            )
        # Repository recovery verification belongs to later execution integration.
        # This phase validates only the persisted project-state transition contract.
        return self._change_project_status(
            state,
            target=ProjectStatus.RUNNING,
            event_type="boss.resume",
            command_name="resume",
            message="Project resumed by Boss",
        )

    def change(self, command: ChangeCommand) -> ChangeResult:
        state = self._store.load()
        self._validate_identity(command.project_id, state)
        if any(
            request.id == command.change_request_id
            for request in state.change_requests
        ):
            raise DuplicateChangeRequest(
                f"change request already exists: {command.change_request_id}"
            )
        if state.project.status not in {
            ProjectStatus.RUNNING,
            ProjectStatus.PAUSED_BY_BOSS,
        }:
            raise InvalidBossCommand(
                f"change is not allowed from {state.project.status}"
            )

        previous_status = state.project.status
        target = ProjectStatus.CHANGE_REQUESTED
        validate_transition(previous_status, target)
        operation_time = self._clock()
        event_id = self._event_id_factory()
        change_request = ChangeRequest(
            id=command.change_request_id,
            project_id=state.project.id,
            description=command.description,
            status=ChangeRequestStatus.PENDING,
            affected_requirement_ids=(),
            created_by=command.created_by,
            created_at=operation_time,
        )
        project = replace(
            state.project,
            status=target,
            updated_at=operation_time,
        )
        event = ProjectEvent(
            id=event_id,
            project_id=state.project.id,
            event_type="boss.change",
            entity_id=change_request.id,
            timestamp=operation_time,
            metadata={"command": "change"},
        )
        new_state = replace(
            state,
            project=project,
            change_requests=state.change_requests + (change_request,),
            events=state.events + (event,),
        )
        self._store.save(new_state)
        return ChangeResult(
            project_id=state.project.id,
            change_request_id=change_request.id,
            previous_status=previous_status,
            current_status=target,
            event_id=event_id,
        )

    def _change_project_status(
        self,
        state: ProjectState,
        *,
        target: ProjectStatus,
        event_type: str,
        command_name: str,
        message: str,
    ) -> CommandResult:
        previous_status = state.project.status
        validate_transition(previous_status, target)
        operation_time = self._clock()
        event_id = self._event_id_factory()
        project = replace(
            state.project,
            status=target,
            updated_at=operation_time,
        )
        event = ProjectEvent(
            id=event_id,
            project_id=state.project.id,
            event_type=event_type,
            entity_id=state.project.id,
            timestamp=operation_time,
            metadata={"command": command_name},
        )
        new_state = replace(
            state,
            project=project,
            events=state.events + (event,),
        )
        self._store.save(new_state)
        return CommandResult(
            project_id=state.project.id,
            previous_status=previous_status,
            current_status=target,
            state_changed=True,
            event_id=event_id,
            message=message,
        )

    @staticmethod
    def _validate_identity(project_id: str, state: ProjectState) -> None:
        if project_id != state.project.id:
            raise ProjectIdentityMismatch(
                f"command project {project_id!r} does not match "
                f"state project {state.project.id!r}"
            )


__all__ = [
    "DuplicateChangeRequest",
    "InvalidBossCommand",
    "OrchestratorService",
    "ProjectIdentityMismatch",
    "ProjectStateStore",
]
