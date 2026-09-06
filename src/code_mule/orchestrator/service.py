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
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.state.models import ProjectState
from code_mule.recovery import (
    BoundaryRecoverability,
    ExecutionPhase,
    ExecutionStopReason,
    SafePointKind,
    WorkerTerminalState,
)
from code_mule.recovery.state import with_stop_boundary

from .commands import ChangeCommand, PauseCommand, QueryCommand, ResumeCommand, StopCommand
from .results import ChangeResult, CommandResult, ProjectStatusView, StopResult


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
        progress_sink: ProgressSink | None = None,
    ):
        self._store = store
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._progress = resilient_progress_sink(progress_sink)

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

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

    def stop(self, command: StopCommand) -> StopResult:
        """Record STOP and cancel now or at the current Task Safe Point."""

        state = self._store.load()
        self._validate_identity(command.project_id, state)
        previous = state.project.status
        if previous is ProjectStatus.CANCELLED:
            return StopResult(
                state.project.id,
                previous,
                previous,
                False,
                False,
                (),
                "Project is already cancelled",
            )
        if previous in {ProjectStatus.DONE, ProjectStatus.FAILED}:
            raise InvalidBossCommand(f"stop is not allowed from {previous}")
        if previous is ProjectStatus.CANCEL_REQUESTED:
            if state.project.current_task_id is None:
                return self.complete_cancellation()
            return StopResult(
                state.project.id,
                previous,
                previous,
                False,
                True,
                (),
                "Project cancellation is already waiting for a Task Safe Point",
            )

        safe_point_required = (
            state.project.current_task_id is not None
            and previous is not ProjectStatus.HUMAN_REQUIRED
        )
        if safe_point_required:
            validate_transition(previous, ProjectStatus.CANCEL_REQUESTED)
            now = self._clock()
            event_id = self._event_id_factory()
            updated = replace(
                state,
                project=replace(
                    state.project,
                    status=ProjectStatus.CANCEL_REQUESTED,
                    updated_at=now,
                ),
                events=state.events
                + (
                    ProjectEvent(
                        id=event_id,
                        project_id=state.project.id,
                        event_type="project.cancel_requested",
                        entity_id=state.project.id,
                        timestamp=now,
                        metadata={"command": "stop", "reason": "boss_requested"},
                    ),
                ),
            )
            updated = self._control_boundary(
                updated, ExecutionStopReason.BOSS_STOP, now
            )
            self._store.save(updated)
            self._emit_cancellation_progress(updated, requested=True)
            return StopResult(
                state.project.id,
                previous,
                ProjectStatus.CANCEL_REQUESTED,
                True,
                True,
                (event_id,),
                "Project cancellation requested",
            )

        requested_id = self._event_id_factory()
        return self._cancel_now(
            state,
            previous_status=previous,
            requested_event_id=requested_id,
            reason="boss_requested",
        )

    def complete_cancellation(self) -> StopResult:
        """Finalize an already-requested cancellation at a Task Safe Point."""

        state = self._store.load()
        if state.project.status is not ProjectStatus.CANCEL_REQUESTED:
            raise InvalidBossCommand(
                f"cancellation completion is not allowed from {state.project.status}"
            )
        if state.project.current_task_id is not None:
            raise InvalidBossCommand("cancellation requires a Task Safe Point")
        return self._cancel_now(
            state,
            previous_status=ProjectStatus.CANCEL_REQUESTED,
            requested_event_id=None,
            reason="Boss requested project cancellation",
        )

    def _cancel_now(
        self,
        state: ProjectState,
        *,
        previous_status: ProjectStatus,
        requested_event_id: str | None,
        reason: str,
    ) -> StopResult:
        validate_transition(previous_status, ProjectStatus.CANCELLED)
        now = self._clock()
        cancelled_id = self._event_id_factory()
        events: tuple[ProjectEvent, ...] = ()
        event_ids: tuple[str, ...] = ()
        if requested_event_id is not None:
            events += (
                ProjectEvent(
                    id=requested_event_id,
                    project_id=state.project.id,
                    event_type="project.cancel_requested",
                    entity_id=state.project.id,
                    timestamp=now,
                    metadata={"command": "stop", "reason": reason},
                ),
            )
            event_ids += (requested_event_id,)
        events += (
            ProjectEvent(
                id=cancelled_id,
                project_id=state.project.id,
                event_type="project.cancelled",
                entity_id=state.project.id,
                timestamp=now,
                metadata={"rollback": "not_performed"},
            ),
        )
        event_ids += (cancelled_id,)
        active_task_ids = self._active_plan_task_ids(state)
        tasks = tuple(
            replace(task, status=TaskStatus.CANCELLED, updated_at=now)
            if task.id in active_task_ids
            and task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
            else task
            for task in state.tasks
        )
        updated = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.CANCELLED,
                current_task_id=None,
                updated_at=now,
            ),
            tasks=tasks,
            events=state.events + events,
        )
        point = updated.latest_safe_point
        updated = with_stop_boundary(
            updated,
            reason=ExecutionStopReason.BOSS_STOP,
            phase=ExecutionPhase.CONTROL,
            safe_point=SafePointKind.UNCERTAIN if point is None else point.kind,
            recoverability=BoundaryRecoverability.TERMINAL,
            worker_started=False,
            worker_terminal_state=WorkerTerminalState.NOT_STARTED,
            report_persisted=False,
            recorded_at=now,
        )
        self._store.save(updated)
        self._emit_cancellation_progress(updated, requested=False)
        return StopResult(
            state.project.id,
            previous_status,
            ProjectStatus.CANCELLED,
            True,
            False,
            event_ids,
            "Project cancelled; completed work was preserved",
        )

    @staticmethod
    def _active_plan_task_ids(state: ProjectState) -> frozenset[str]:
        plan_id = state.project.active_plan_id
        if plan_id is None:
            return frozenset()
        plan = next((item for item in state.plans if item.id == plan_id), None)
        if plan is None:
            return frozenset()
        milestone_ids = set(plan.milestone_ids)
        return frozenset(
            task_id
            for milestone in state.milestones
            if milestone.plan_id == plan.id and milestone.id in milestone_ids
            for task_id in milestone.task_ids
        )

    def _emit_cancellation_progress(
        self, state: ProjectState, *, requested: bool
    ) -> None:
        self._progress.emit(
            ProgressEvent(
                type=(
                    ProgressEventType.PROJECT_CANCELLATION_REQUESTED
                    if requested
                    else ProgressEventType.PROJECT_CANCELLED
                ),
                timestamp=self._clock(),
                project_id=state.project.id,
                task_id=state.project.current_task_id,
                attempt=None,
                message=(
                    "Project cancellation requested"
                    if requested
                    else "Project cancelled"
                ),
                metadata={"project_status": state.project.status.value},
            )
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
        new_state = self._control_boundary(
            new_state, ExecutionStopReason.CHANGE_REQUESTED, operation_time
        )
        self._store.save(new_state)
        message = (
            "Change requested; finishing current task..."
            if state.project.current_task_id is not None
            else "Change requested"
        )
        self._progress.emit(
            ProgressEvent(
                type=ProgressEventType.CHANGE_REQUESTED,
                timestamp=operation_time,
                project_id=state.project.id,
                task_id=state.project.current_task_id,
                attempt=None,
                message=message,
                metadata={"change_request_id": change_request.id},
            )
        )
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
        if target is ProjectStatus.PAUSED_BY_BOSS:
            new_state = self._control_boundary(
                new_state, ExecutionStopReason.BOSS_PAUSE, operation_time
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
    def _control_boundary(
        state: ProjectState, reason: ExecutionStopReason, operation_time: datetime
    ) -> ProjectState:
        point = state.latest_safe_point
        kind = SafePointKind.UNCERTAIN if point is None else point.kind
        attempts = tuple(
            item for item in state.execution_attempts
            if item.task_id == state.project.current_task_id
        )
        latest = max(attempts, key=lambda item: item.attempt, default=None)
        worker_started = latest is not None and latest.status.value != "prepared"
        return with_stop_boundary(
            state,
            reason=reason,
            phase=ExecutionPhase.CONTROL,
            safe_point=kind,
            recoverability=BoundaryRecoverability.RECOVERABLE,
            worker_started=worker_started,
            worker_terminal_state=(
                WorkerTerminalState.UNKNOWN
                if worker_started
                else WorkerTerminalState.NOT_STARTED
            ),
            report_persisted=False,
            recorded_at=operation_time,
            task_id=state.project.current_task_id,
            attempt=None if latest is None else latest.attempt,
            head_sha=None if latest is None else latest.baseline_head,
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
