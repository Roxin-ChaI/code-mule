"""Deterministic execution of the already-materialized active Plan."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import PlanStatus, ProjectStatus, TaskStatus
from code_mule.domain.models import Milestone, ProjectEvent, Task
from code_mule.domain.state_machine import validate_transition
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.scheduler import SchedulerError, TaskScheduler
from code_mule.scheduler.selection import resolve_active_plan_graph
from code_mule.state.models import ProjectState

from .contracts import (
    InvalidProjectExecutionState,
    ProjectExecutionConfig,
    ProjectExecutionOutcome,
    ProjectExecutionStopReason,
    TaskCycleOutcome,
    TaskCycleRequest,
)


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...

    def save(self, state: ProjectState) -> None: ...


class TaskCycleRunner(Protocol):
    def execute(self, request: TaskCycleRequest) -> TaskCycleOutcome: ...


class TaskPromptBuilder:
    """Build a bounded initial prompt without serializing ProjectState."""

    def build(self, state: ProjectState, task: Task) -> str:
        direct_tasks = {
            item.id: item for item in state.tasks if item.id in task.dependencies
        }
        completed_dependencies = tuple(
            direct_tasks[dependency_id]
            for dependency_id in task.dependencies
            if dependency_id in direct_tasks
            and direct_tasks[dependency_id].status is TaskStatus.COMPLETED
        )
        criteria = "\n".join(
            f"- {criterion}" for criterion in task.acceptance_criteria
        ) or "- None specified"
        dependencies = ", ".join(task.dependencies) or "None"
        completed = "\n".join(
            f"- {dependency.id}: {dependency.title}"
            for dependency in completed_dependencies
        ) or "- None"
        return (
            "Execute this Code Mule task in the provided repository.\n\n"
            f"Project: {state.project.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task description: {task.description}\n\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"Dependency IDs: {dependencies}\n"
            f"Completed direct dependencies:\n{completed}\n\n"
            "Execution constraints:\n"
            "- Work only on this task and its acceptance criteria.\n"
            "- Inspect existing repository state before editing.\n"
            "- Run focused verification and do not push, tag, or release.\n"
            "- Stop for any operation requiring human approval.\n\n"
            "The Worker layer enforces its structured report contract through "
            "the native output schema; do not invent another control protocol."
        )


class ProjectExecutionService:
    """Run Ready Tasks sequentially, persisting every side-effect boundary."""

    def __init__(
        self,
        *,
        store: ProjectStateStore,
        scheduler: TaskScheduler,
        task_cycle_factory: Callable[[], TaskCycleRunner],
        prompt_builder: TaskPromptBuilder,
        clock: Callable[[], datetime],
        event_id_factory: Callable[[], str],
        config: ProjectExecutionConfig,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._task_cycle_factory = task_cycle_factory
        self._prompt_builder = prompt_builder
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._config = config
        self._progress = resilient_progress_sink(progress_sink)

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def run(self) -> ProjectExecutionOutcome:
        initial = self._store.load()
        completed, total = self._progress_counts(initial)
        self._emit_progress(
            initial,
            ProgressEventType.PROJECT_STARTED,
            "Project execution started",
            metadata={
                "project_name": initial.project.name,
                "project_status": initial.project.status.value,
                "completed_tasks": str(completed),
                "total_tasks": str(total),
            },
        )
        try:
            return self._run()
        except BaseException as error:
            latest = self._store.load()
            self._emit_progress(
                latest,
                ProgressEventType.ERROR,
                "Project execution failed",
                metadata={"error_type": type(error).__name__},
            )
            raise

    def _run(self) -> ProjectExecutionOutcome:
        started = 0
        completed = 0
        task_ids: tuple[str, ...] = ()

        while True:
            state = self._store.load()
            boundary = self._boundary_outcome(state, started, completed, task_ids)
            if boundary is not None:
                self._emit_stopped(state, boundary.stop_reason)
                return boundary

            self._scheduler.validate(state)
            if state.project.current_task_id is not None:
                return self._recovery_required(state, started, completed, task_ids)

            if self._scheduler.is_plan_complete(state):
                final_state = self._complete_plan(state)
                completed_count, total = self._progress_counts_from_closed_plan(
                    final_state
                )
                completion_metadata = {
                    "completed_tasks": str(completed_count),
                    "total_tasks": str(total),
                }
                self._emit_progress(
                    final_state,
                    ProgressEventType.PLAN_COMPLETED,
                    "Active Plan completed",
                    metadata=completion_metadata,
                )
                self._emit_progress(
                    final_state,
                    ProgressEventType.PROJECT_COMPLETED,
                    "Project completed",
                    metadata=completion_metadata,
                )
                return self._outcome(
                    final_state,
                    started,
                    completed,
                    task_ids,
                    ProjectExecutionStopReason.PLAN_COMPLETED,
                    plan_completed=True,
                )

            if started >= self._config.max_tasks_per_run:
                stopped = self._stop_for_human(
                    state,
                    "project.execution_limit_reached",
                    {"max_tasks": str(self._config.max_tasks_per_run)},
                )
                self._emit_stopped(
                    stopped, ProjectExecutionStopReason.TASK_LIMIT_REACHED
                )
                return self._outcome(
                    stopped,
                    started,
                    completed,
                    task_ids,
                    ProjectExecutionStopReason.TASK_LIMIT_REACHED,
                    human_action_required=True,
                )

            task = self._scheduler.select_next(state)
            if task is None:
                stopped = self._stop_for_human(
                    state, "project.no_runnable_task", {}
                )
                self._emit_stopped(
                    stopped, ProjectExecutionStopReason.NO_RUNNABLE_TASK
                )
                return self._outcome(
                    stopped,
                    started,
                    completed,
                    task_ids,
                    ProjectExecutionStopReason.NO_RUNNABLE_TASK,
                    human_action_required=True,
                )

            dispatched = self._dispatch(state, task)
            completed_before, total = self._progress_counts(dispatched)
            self._emit_progress(
                dispatched,
                ProgressEventType.TASK_DISPATCHED,
                f"Dispatched {task.title}",
                task_id=task.id,
                metadata={
                    "task_title": task.title,
                    "completed_tasks": str(completed_before),
                    "total_tasks": str(total),
                },
            )
            started += 1
            task_ids += (task.id,)
            current = self._task(dispatched, task.id)
            prompt = self._prompt_builder.build(dispatched, current)
            cycle_outcome = self._task_cycle_factory().execute(
                TaskCycleRequest(current, prompt)
            )
            latest = self._store.load()
            if cycle_outcome.human_action_required:
                latest = self._ensure_human_required(latest, task.id)
                self._emit_stopped(
                    latest, ProjectExecutionStopReason.HUMAN_REQUIRED
                )
                return self._outcome(
                    latest,
                    started,
                    completed,
                    task_ids,
                    ProjectExecutionStopReason.HUMAN_REQUIRED,
                    human_action_required=True,
                )

            persisted_task = self._task(latest, task.id)
            if (
                persisted_task.status is not TaskStatus.COMPLETED
                or latest.project.current_task_id is not None
            ):
                raise InvalidProjectExecutionState(
                    "TaskCycle success must persist COMPLETED and clear current_task_id"
                )
            completed += 1
            completed_count, total = self._progress_counts(latest)
            self._emit_progress(
                latest,
                ProgressEventType.TASK_COMPLETED,
                f"Completed {persisted_task.title}",
                task_id=persisted_task.id,
                attempt=persisted_task.execution_attempts or None,
                metadata={
                    "task_title": persisted_task.title,
                    "completed_tasks": str(completed_count),
                    "total_tasks": str(total),
                },
            )
            self._complete_ready_milestones(latest)

    def _boundary_outcome(
        self,
        state: ProjectState,
        started: int,
        completed: int,
        task_ids: tuple[str, ...],
    ) -> ProjectExecutionOutcome | None:
        status = state.project.status
        if status is ProjectStatus.RUNNING:
            return None
        reasons = {
            ProjectStatus.PAUSED_BY_BOSS: ProjectExecutionStopReason.PAUSED,
            ProjectStatus.CHANGE_REQUESTED: ProjectExecutionStopReason.CHANGE_REQUESTED,
            ProjectStatus.HUMAN_REQUIRED: ProjectExecutionStopReason.HUMAN_REQUIRED,
            ProjectStatus.DONE: ProjectExecutionStopReason.PLAN_COMPLETED,
            ProjectStatus.FAILED: ProjectExecutionStopReason.TASK_CYCLE_STOPPED,
        }
        if status not in reasons:
            raise InvalidProjectExecutionState(
                f"project execution is not allowed from {status}"
            )
        return self._outcome(
            state,
            started,
            completed,
            task_ids,
            reasons[status],
            plan_completed=status is ProjectStatus.DONE,
            human_action_required=status in {
                ProjectStatus.HUMAN_REQUIRED,
                ProjectStatus.FAILED,
            },
        )

    def _dispatch(self, state: ProjectState, task: Task) -> ProjectState:
        if task.status not in {TaskStatus.PENDING, TaskStatus.REOPENED}:
            raise InvalidProjectExecutionState("only a Ready Task may be dispatched")
        operation_time = self._clock()
        updated_task = replace(
            task, status=TaskStatus.IN_PROGRESS, updated_at=operation_time
        )
        event = self._event(
            state,
            "task.dispatched",
            task.id,
            operation_time,
            {"previous_status": task.status.value},
        )
        updated = replace(
            state,
            project=replace(
                state.project,
                current_task_id=task.id,
                updated_at=operation_time,
            ),
            tasks=self._replace_task(state.tasks, updated_task),
            events=state.events + (event,),
        )
        self._store.save(updated)
        return updated

    def _recovery_required(
        self,
        state: ProjectState,
        started: int,
        completed: int,
        task_ids: tuple[str, ...],
    ) -> ProjectExecutionOutcome:
        current = self._task(state, state.project.current_task_id or "")
        if current.status is not TaskStatus.IN_PROGRESS:
            raise InvalidProjectExecutionState(
                "project.current_task_id must reference an IN_PROGRESS Task"
            )
        stopped = self._stop_for_human(
            state,
            "project.execution_recovery_required",
            {"reason": "execution_ownership_uncertain"},
            entity_id=current.id,
        )
        self._emit_stopped(stopped, ProjectExecutionStopReason.HUMAN_REQUIRED)
        return self._outcome(
            stopped,
            started,
            completed,
            task_ids,
            ProjectExecutionStopReason.HUMAN_REQUIRED,
            human_action_required=True,
        )

    def _complete_ready_milestones(self, state: ProjectState) -> ProjectState:
        graph = resolve_active_plan_graph(state)
        tasks = {task.id: task for task in graph.tasks}
        operation_time = self._clock()
        milestones = state.milestones
        events: tuple[ProjectEvent, ...] = ()
        for milestone in graph.milestones:
            if milestone.status == "completed":
                continue
            if all(
                tasks[task_id].status
                in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
                for task_id in milestone.task_ids
            ):
                milestones = self._replace_milestone(
                    milestones, replace(milestone, status="completed")
                )
                events += (
                    self._event(
                        state,
                        "milestone.completed",
                        milestone.id,
                        operation_time,
                        {},
                    ),
                )
        if not events:
            return state
        updated = replace(
            state,
            project=replace(state.project, updated_at=operation_time),
            milestones=milestones,
            events=state.events + events,
        )
        self._store.save(updated)
        return updated

    def _complete_plan(self, state: ProjectState) -> ProjectState:
        graph = resolve_active_plan_graph(state)
        operation_time = self._clock()
        tasks = {task.id: task for task in graph.tasks}
        milestones = state.milestones
        milestone_events: tuple[ProjectEvent, ...] = ()
        for milestone in graph.milestones:
            if milestone.status == "completed":
                continue
            if not all(
                tasks[task_id].status
                in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
                for task_id in milestone.task_ids
            ):
                raise InvalidProjectExecutionState(
                    "Plan completion found an incomplete Milestone"
                )
            milestones = self._replace_milestone(
                milestones, replace(milestone, status="completed")
            )
            milestone_events += (
                self._event(
                    state,
                    "milestone.completed",
                    milestone.id,
                    operation_time,
                    {},
                ),
            )
        validate_transition(state.project.status, ProjectStatus.DONE)
        completed_plan = replace(graph.plan, status=PlanStatus.COMPLETED)
        plan_event = self._event(
            state, "plan.completed", graph.plan.id, operation_time, {}
        )
        project_event = self._event(
            state, "project.completed", state.project.id, operation_time, {}
        )
        updated = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.DONE,
                current_task_id=None,
                updated_at=operation_time,
            ),
            plans=tuple(
                completed_plan if plan.id == completed_plan.id else plan
                for plan in state.plans
            ),
            milestones=milestones,
            events=state.events + milestone_events + (plan_event, project_event),
        )
        self._store.save(updated)
        return updated

    def _ensure_human_required(
        self, state: ProjectState, task_id: str
    ) -> ProjectState:
        if state.project.status is ProjectStatus.HUMAN_REQUIRED:
            return state
        if state.project.status is not ProjectStatus.RUNNING:
            raise InvalidProjectExecutionState(
                "TaskCycle human outcome left an incompatible project status"
            )
        return self._stop_for_human(
            state,
            "project.task_cycle_stopped",
            {"reason": "task_cycle_human_required"},
            entity_id=task_id,
        )

    def _stop_for_human(
        self,
        state: ProjectState,
        event_type: str,
        metadata: dict[str, str],
        *,
        entity_id: str | None = None,
    ) -> ProjectState:
        validate_transition(state.project.status, ProjectStatus.HUMAN_REQUIRED)
        operation_time = self._clock()
        event = self._event(
            state,
            event_type,
            entity_id or state.project.id,
            operation_time,
            metadata,
        )
        updated = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.HUMAN_REQUIRED,
                updated_at=operation_time,
            ),
            events=state.events + (event,),
        )
        self._store.save(updated)
        return updated

    def _emit_stopped(
        self,
        state: ProjectState,
        reason: ProjectExecutionStopReason,
    ) -> None:
        completed, total = self._progress_counts(state)
        self._emit_progress(
            state,
            ProgressEventType.PROJECT_STOPPED,
            f"Project execution stopped: {reason.value}",
            task_id=state.project.current_task_id,
            metadata={
                "reason": reason.value,
                "project_status": state.project.status.value,
                "completed_tasks": str(completed),
                "total_tasks": str(total),
            },
        )

    def _emit_progress(
        self,
        state: ProjectState,
        event_type: ProgressEventType,
        message: str,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._progress.emit(
            ProgressEvent(
                type=event_type,
                timestamp=self._clock(),
                project_id=state.project.id,
                task_id=task_id,
                attempt=attempt,
                message=message,
                metadata=metadata or {},
            )
        )

    @staticmethod
    def _progress_counts(state: ProjectState) -> tuple[int, int]:
        try:
            graph = resolve_active_plan_graph(state)
        except SchedulerError:
            return 0, 0
        return (
            sum(task.status is TaskStatus.COMPLETED for task in graph.tasks),
            len(graph.tasks),
        )

    @staticmethod
    def _progress_counts_from_closed_plan(state: ProjectState) -> tuple[int, int]:
        active_plan_id = state.project.active_plan_id
        plan = next(
            (item for item in state.plans if item.id == active_plan_id), None
        )
        if plan is None:
            return 0, 0
        milestone_ids = set(plan.milestone_ids)
        task_ids = {
            task_id
            for milestone in state.milestones
            if milestone.id in milestone_ids and milestone.plan_id == plan.id
            for task_id in milestone.task_ids
        }
        tasks = tuple(task for task in state.tasks if task.id in task_ids)
        return (
            sum(task.status is TaskStatus.COMPLETED for task in tasks),
            len(tasks),
        )

    def _event(
        self,
        state: ProjectState,
        event_type: str,
        entity_id: str,
        timestamp: datetime,
        metadata: dict[str, str],
    ) -> ProjectEvent:
        return ProjectEvent(
            id=self._event_id_factory(),
            project_id=state.project.id,
            event_type=event_type,
            entity_id=entity_id,
            timestamp=timestamp,
            metadata=metadata,
        )

    @staticmethod
    def _task(state: ProjectState, task_id: str) -> Task:
        matches = tuple(task for task in state.tasks if task.id == task_id)
        if len(matches) != 1:
            raise InvalidProjectExecutionState(
                f"task {task_id!r} must exist exactly once in ProjectState"
            )
        return matches[0]

    @staticmethod
    def _replace_task(tasks: tuple[Task, ...], updated: Task) -> tuple[Task, ...]:
        return tuple(updated if task.id == updated.id else task for task in tasks)

    @staticmethod
    def _replace_milestone(
        milestones: tuple[Milestone, ...], updated: Milestone
    ) -> tuple[Milestone, ...]:
        return tuple(
            updated if milestone.id == updated.id else milestone
            for milestone in milestones
        )

    @staticmethod
    def _outcome(
        state: ProjectState,
        started: int,
        completed: int,
        task_ids: tuple[str, ...],
        reason: ProjectExecutionStopReason,
        *,
        plan_completed: bool = False,
        human_action_required: bool = False,
    ) -> ProjectExecutionOutcome:
        return ProjectExecutionOutcome(
            project_id=state.project.id,
            tasks_started=started,
            tasks_completed=completed,
            task_ids=task_ids,
            final_project_status=state.project.status,
            plan_completed=plan_completed,
            human_action_required=human_action_required,
            stop_reason=reason,
        )


__all__ = [
    "ProjectExecutionService",
    "ProjectStateStore",
    "TaskCycleRunner",
    "TaskPromptBuilder",
]
