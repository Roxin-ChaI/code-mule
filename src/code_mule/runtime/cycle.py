"""Deterministic control flow for one current task and one Worker thread."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import (
    HumanActionCategory,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import Decision, ExecutionReport, ProjectEvent, Task
from code_mule.domain.state_machine import validate_transition
from code_mule.human import request_human_action
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.state.models import ProjectState
from code_mule.supervisor.contracts import ReviewRequest, ReviewResult
from code_mule.worker.contracts import (
    CodexApprovalRequired,
    CodexUserInputRequired,
    CodexWorkerError,
    WorkerTaskRequest,
)

from .contracts import (
    InvalidTaskCycleState,
    TaskCycleConfig,
    TaskCycleOutcome,
    TaskCycleRequest,
)


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...

    def save(self, state: ProjectState) -> None: ...


class WorkerSession(Protocol):
    @property
    def thread_id(self) -> str | None: ...

    def start(self) -> None: ...

    def execute(
        self,
        request: WorkerTaskRequest,
        *,
        report_id: str,
        created_at: datetime,
    ) -> ExecutionReport: ...

    def close(self) -> None: ...


class ReviewService(Protocol):
    def review(self, request: ReviewRequest) -> ReviewResult: ...


class TaskCycleService:
    """Run one task until a deterministic terminal review outcome."""

    def __init__(
        self,
        *,
        worker_session_factory: Callable[[], WorkerSession],
        supervisor: ReviewService,
        store: ProjectStateStore,
        clock: Callable[[], datetime],
        report_id_factory: Callable[[], str],
        decision_id_factory: Callable[[], str],
        event_id_factory: Callable[[], str],
        config: TaskCycleConfig,
        progress_sink: ProgressSink | None = None,
        worker_identity_started: Callable[[str, str, int], None] | None = None,
        worker_identity_cleared: Callable[[str], None] | None = None,
    ) -> None:
        self._worker_session_factory = worker_session_factory
        self._supervisor = supervisor
        self._store = store
        self._clock = clock
        self._report_id_factory = report_id_factory
        self._decision_id_factory = decision_id_factory
        self._event_id_factory = event_id_factory
        self._config = config
        self._progress = resilient_progress_sink(progress_sink)
        self._worker_identity_started = worker_identity_started
        self._worker_identity_cleared = worker_identity_cleared

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def execute(self, request: TaskCycleRequest) -> TaskCycleOutcome:
        state = self._store.load()
        task = self._validate_start(state, request)
        session = self._worker_session_factory()
        reports: tuple[ExecutionReport, ...] = ()
        decisions: tuple[Decision, ...] = ()
        prompt = request.initial_prompt

        try:
            try:
                self._emit_progress(
                    state,
                    task,
                    ProgressEventType.WORKER_STARTING,
                    "Codex Worker starting",
                )
                session.start()
                self._emit_progress(
                    state,
                    task,
                    ProgressEventType.WORKER_STARTED,
                    "Codex Worker started",
                )
            except CodexWorkerError as error:
                self._record_worker_failure(state, task, error)
                self._emit_worker_failure(state, task, error)
                return self._human_outcome(
                    task.id, reports, decisions, final_prompt=None
                )
            while True:
                task = self._task(state, request.task.id)
                state = self._record_execution_started(state, task)
                attempt = task.execution_attempts + 1
                if self._worker_identity_started is not None:
                    thread_id = session.thread_id
                    if thread_id in (None, ""):
                        raise InvalidTaskCycleState(
                            "started Worker session must expose a Codex thread ID"
                        )
                    self._worker_identity_started(task.id, thread_id, attempt)
                    # The ownership callback persists Codex recovery identity in
                    # ProjectState. Continue from that latest snapshot so a
                    # pre-report Worker failure cannot overwrite the identity.
                    state = self._store.load()
                    task = self._task(state, task.id)
                self._emit_progress(
                    state,
                    task,
                    ProgressEventType.TASK_STARTED,
                    f"Task attempt {attempt} started",
                    attempt=attempt,
                )
                worker_request = WorkerTaskRequest(task, prompt, task.title)
                try:
                    report = session.execute(
                        worker_request,
                        report_id=self._report_id_factory(),
                        created_at=self._clock(),
                    )
                except CodexWorkerError as error:
                    state = self._record_worker_failure(state, task, error)
                    self._emit_worker_failure(state, task, error)
                    return self._human_outcome(
                        task.id,
                        reports,
                        decisions,
                        final_prompt=None,
                    )

                self._emit_progress(
                    state,
                    task,
                    ProgressEventType.WORKER_COMPLETED,
                    "Codex Worker completed",
                    attempt=report.attempt,
                    metadata={"status": report.status},
                )

                # A Boss CHANGE may be persisted while the Worker owns this Task.
                # Reload at the Safe Point so report persistence preserves that
                # authoritative status and ChangeRequest instead of overwriting it
                # with the pre-Worker snapshot.
                state = self._store.load()
                task = self._task(state, task.id)
                state = self._persist_report(state, task, report)
                reports += (report,)

                if report.human_action_required:
                    self._transition_human_required(
                        state,
                        self._task(state, task.id),
                        event_types=("task.human_required",),
                        metadata={"source": "worker_report"},
                        category=HumanActionCategory.EXTERNAL_SIDE_EFFECT,
                        summary="Worker reported an operation requiring human review",
                        requested_action="Inspect and explicitly approve or reject the specific operation",
                        risk="The operation may have an irreversible external side effect",
                    )
                    self._emit_human_gate(
                        state,
                        self._task(state, task.id),
                        "worker_report",
                        attempt=report.attempt,
                    )
                    return self._human_outcome(
                        task.id, reports, decisions, final_prompt=None
                    )

                persisted = self._store.load()
                persisted_task = self._task(persisted, task.id)
                self._emit_progress(
                    persisted,
                    persisted_task,
                    ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                    "Supervisor reviewing",
                    attempt=report.attempt,
                )
                try:
                    review = self._supervisor.review(
                        ReviewRequest(persisted, persisted_task, report)
                    )
                except BaseException as error:
                    self._emit_progress(
                        persisted,
                        persisted_task,
                        ProgressEventType.SUPERVISOR_FAILED,
                        "Supervisor review failed",
                        attempt=report.attempt,
                        metadata={"error_type": type(error).__name__},
                    )
                    self._transition_human_required(
                        persisted,
                        persisted_task,
                        event_types=("supervisor.review_failed", "task.human_required"),
                        metadata={"error_type": type(error).__name__},
                        category=HumanActionCategory.SUPERVISOR_FAILURE,
                        summary="Supervisor review failed",
                        requested_action="Inspect the failure and choose an explicit resolution",
                        risk="Execution cannot continue without a trustworthy Supervisor decision",
                    )
                    raise
                self._emit_progress(
                    persisted,
                    persisted_task,
                    ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
                    f"Supervisor decision: {review.decision.value}",
                    attempt=report.attempt,
                    metadata={"decision": review.decision.value},
                )
                state, decision = self._persist_decision(persisted, persisted_task, review)
                decisions += (decision,)

                if review.decision is SupervisorDecisionType.CONTINUE:
                    self._complete_task(state, persisted_task, review.decision)
                    self._clear_worker_identity(task.id)
                    self._emit_progress(
                        state,
                        persisted_task,
                        ProgressEventType.TASK_COMPLETED,
                        "Task completed",
                        attempt=report.attempt,
                        metadata={"decision": review.decision.value},
                    )
                    return TaskCycleOutcome(
                        task_id=task.id,
                        attempts=len(reports),
                        final_decision=review.decision,
                        execution_reports=reports,
                        decisions=decisions,
                        final_prompt=None,
                        human_action_required=False,
                    )

                if review.decision is SupervisorDecisionType.DONE:
                    self._complete_task(state, persisted_task, review.decision)
                    self._clear_worker_identity(task.id)
                    self._emit_progress(
                        state,
                        persisted_task,
                        ProgressEventType.TASK_COMPLETED,
                        "Task completed",
                        attempt=report.attempt,
                        metadata={"decision": review.decision.value},
                    )
                    return TaskCycleOutcome(
                        task_id=task.id,
                        attempts=len(reports),
                        final_decision=review.decision,
                        execution_reports=reports,
                        decisions=decisions,
                        final_prompt=None,
                        human_action_required=False,
                    )

                if review.decision is SupervisorDecisionType.HUMAN_REQUIRED:
                    self._transition_human_required(
                        state,
                        persisted_task,
                        event_types=("task.human_required",),
                        metadata={"source": "supervisor"},
                        category=HumanActionCategory.SUPERVISOR_FAILURE,
                        summary="Supervisor requested human judgment",
                        requested_action="Inspect the task outcome and choose an explicit resolution",
                        risk="Continuing without human judgment may violate project constraints",
                    )
                    self._emit_human_gate(
                        state,
                        persisted_task,
                        "supervisor",
                        attempt=report.attempt,
                    )
                    return self._human_outcome(
                        task.id, reports, decisions, final_prompt=None
                    )

                if review.decision is not SupervisorDecisionType.REWORK:
                    raise InvalidTaskCycleState(
                        f"unsupported Supervisor decision: {review.decision}"
                    )

                prompt = review.next_task_prompt
                if prompt in (None, ""):
                    raise InvalidTaskCycleState("REWORK requires next_task_prompt")
                state = self._record_rework(state, persisted_task, decision)
                self._emit_progress(
                    state,
                    persisted_task,
                    ProgressEventType.TASK_REWORK,
                    "Task rework requested",
                    attempt=report.attempt,
                    metadata={"decision": review.decision.value},
                )
                if len(reports) >= self._config.max_attempts:
                    self._transition_human_required(
                        state,
                        self._task(state, task.id),
                        event_types=(
                            "task.cycle_limit_reached",
                            "task.human_required",
                        ),
                        metadata={"max_attempts": str(self._config.max_attempts)},
                        category=HumanActionCategory.ATTEMPT_LIMIT,
                        summary="Task reached its configured attempt limit",
                        requested_action="Choose whether to retry the task or stop the project",
                        risk="Retrying may repeat work performed by an earlier attempt",
                    )
                    self._emit_human_gate(
                        state,
                        self._task(state, task.id),
                        "task_cycle_limit",
                        attempt=report.attempt,
                    )
                    return self._human_outcome(
                        task.id,
                        reports,
                        decisions,
                        final_prompt=prompt,
                    )
        finally:
            session.close()

    def _clear_worker_identity(self, task_id: str) -> None:
        if self._worker_identity_cleared is not None:
            self._worker_identity_cleared(task_id)

    def _emit_worker_failure(
        self, state: ProjectState, task: Task, error: CodexWorkerError
    ) -> None:
        attempt = task.execution_attempts + 1
        self._emit_progress(
            state,
            task,
            ProgressEventType.WORKER_FAILED,
            "Codex Worker failed",
            attempt=attempt,
            metadata={"error_type": type(error).__name__},
        )
        self._emit_human_gate(
            state, task, "worker_failure", attempt=attempt
        )

    def _emit_human_gate(
        self,
        state: ProjectState,
        task: Task,
        source: str,
        *,
        attempt: int,
    ) -> None:
        metadata = {"source": source}
        self._emit_progress(
            state,
            task,
            ProgressEventType.TASK_HUMAN_REQUIRED,
            "Task requires human action",
            attempt=attempt,
            metadata=metadata,
        )
        self._emit_progress(
            state,
            task,
            ProgressEventType.HUMAN_GATE,
            "Human action required",
            attempt=attempt,
            metadata=metadata,
        )

    def _emit_progress(
        self,
        state: ProjectState,
        task: Task,
        event_type: ProgressEventType,
        message: str,
        *,
        attempt: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._progress.emit(
            ProgressEvent(
                type=event_type,
                timestamp=self._clock(),
                project_id=state.project.id,
                task_id=task.id,
                attempt=attempt,
                message=message,
                metadata=metadata or {},
            )
        )

    def _validate_start(
        self, state: ProjectState, request: TaskCycleRequest
    ) -> Task:
        matches = tuple(task for task in state.tasks if task.id == request.task.id)
        if len(matches) != 1:
            raise InvalidTaskCycleState(
                f"task {request.task.id!r} must exist exactly once in ProjectState"
            )
        task = matches[0]
        if state.project.current_task_id != task.id:
            raise InvalidTaskCycleState("task is not the current project task")
        if task.status is not TaskStatus.IN_PROGRESS:
            raise InvalidTaskCycleState("current task must be IN_PROGRESS")
        if state.project.status is not ProjectStatus.RUNNING:
            raise InvalidTaskCycleState("project must be RUNNING")
        return task

    @staticmethod
    def _task(state: ProjectState, task_id: str) -> Task:
        matches = tuple(task for task in state.tasks if task.id == task_id)
        if len(matches) != 1:
            raise InvalidTaskCycleState(
                f"task {task_id!r} must exist exactly once in ProjectState"
            )
        return matches[0]

    def _record_execution_started(
        self, state: ProjectState, task: Task
    ) -> ProjectState:
        state, task = self._reload_task_state(task.id)
        operation_time = self._clock()
        event = self._event(
            state,
            task,
            "task.execution_started",
            operation_time,
            {"attempt": str(task.execution_attempts + 1)},
        )
        new_state = replace(
            state,
            project=replace(state.project, updated_at=operation_time),
            events=state.events + (event,),
        )
        self._store.save(new_state)
        return new_state

    def _persist_report(
        self,
        state: ProjectState,
        task: Task,
        report: ExecutionReport,
    ) -> ProjectState:
        state, task = self._reload_task_state(task.id)
        if report.task_id != task.id:
            raise InvalidTaskCycleState("Worker report targets a different task")
        if report.attempt != task.execution_attempts + 1:
            raise InvalidTaskCycleState("Worker report attempt is not sequential")
        if any(item.id == report.id for item in state.execution_reports):
            raise InvalidTaskCycleState("Worker report ID already exists")
        updated_task = replace(
            task,
            execution_attempts=report.attempt,
            updated_at=report.created_at,
        )
        event_type = (
            "task.execution_completed"
            if report.status == "completed"
            else "task.execution_failed"
        )
        event = self._event(
            state,
            task,
            event_type,
            report.created_at,
            {"report_id": report.id, "status": report.status},
        )
        new_state = replace(
            state,
            project=replace(state.project, updated_at=report.created_at),
            tasks=self._replace_task(state.tasks, updated_task),
            execution_reports=state.execution_reports + (report,),
            events=state.events + (event,),
        )
        self._store.save(new_state)
        return new_state

    def _persist_decision(
        self,
        state: ProjectState,
        task: Task,
        review: ReviewResult,
    ) -> tuple[ProjectState, Decision]:
        # Supervisor review is an external-call window. A Boss command may have
        # persisted a control status while it was running, so merge this
        # TaskCycle-owned Decision into the latest source-of-truth snapshot.
        state, task = self._reload_task_state(task.id)
        operation_time = self._clock()
        decision = Decision(
            id=self._decision_id_factory(),
            task_id=task.id,
            type=review.decision,
            rationale=review.rationale,
            created_at=operation_time,
        )
        if any(item.id == decision.id for item in state.decisions):
            raise InvalidTaskCycleState("Supervisor decision ID already exists")
        event = self._event(
            state,
            task,
            "supervisor.review_completed",
            operation_time,
            {"decision": decision.type.value, "decision_id": decision.id},
        )
        new_state = replace(
            state,
            project=replace(state.project, updated_at=operation_time),
            decisions=state.decisions + (decision,),
            events=state.events + (event,),
        )
        self._store.save(new_state)
        return new_state, decision

    def _record_rework(
        self, state: ProjectState, task: Task, decision: Decision
    ) -> ProjectState:
        state, task = self._reload_task_state(task.id)
        operation_time = self._clock()
        event = self._event(
            state,
            task,
            "task.rework_requested",
            operation_time,
            {"decision_id": decision.id},
        )
        new_state = replace(
            state,
            project=replace(state.project, updated_at=operation_time),
            events=state.events + (event,),
        )
        self._store.save(new_state)
        return new_state

    def _complete_task(
        self,
        state: ProjectState,
        task: Task,
        decision: SupervisorDecisionType,
    ) -> ProjectState:
        # TaskCycle owns completion and current_task_id clearing, but not the
        # Project control status. Reload immediately before the Safe-Point save
        # so CHANGE_REQUESTED, PAUSED_BY_BOSS, or HUMAN_REQUIRED survives.
        state, task = self._reload_task_state(task.id)
        operation_time = self._clock()
        current_task = self._task(state, task.id)
        completed_task = replace(
            current_task,
            status=TaskStatus.COMPLETED,
            updated_at=operation_time,
        )
        event = self._event(
            state,
            current_task,
            "task.completed",
            operation_time,
            {
                "decision": decision.value,
                "project_completion": (
                    "higher_level_required"
                    if decision is SupervisorDecisionType.DONE
                    else "not_requested"
                ),
            },
        )
        new_state = replace(
            state,
            project=replace(
                state.project,
                current_task_id=None,
                updated_at=operation_time,
            ),
            tasks=self._replace_task(state.tasks, completed_task),
            events=state.events + (event,),
        )
        self._store.save(new_state)
        return new_state

    def _record_worker_failure(
        self, state: ProjectState, task: Task, error: CodexWorkerError
    ) -> ProjectState:
        category, summary, requested_action, risk = self._worker_failure_action(error)
        return self._transition_human_required(
            state,
            task,
            event_types=("task.execution_failed", "task.human_required"),
            metadata={"error_type": type(error).__name__},
            category=category,
            summary=summary,
            requested_action=requested_action,
            risk=risk,
        )

    @staticmethod
    def _worker_failure_action(error: CodexWorkerError):
        if isinstance(error, CodexApprovalRequired):
            return (
                HumanActionCategory.WORKER_APPROVAL,
                "Codex Worker requested approval",
                "Review and approve or reject this specific Worker request",
                "Approval may authorize an external or destructive operation",
            )
        if isinstance(error, CodexUserInputRequired):
            return (
                HumanActionCategory.WORKER_INPUT,
                "Codex Worker requires human input",
                "Provide the required input, then choose an explicit resolution",
                "The original Worker session cannot be resumed automatically",
            )
        return (
            HumanActionCategory.RECOVERY_UNCERTAIN,
            "Codex Worker stopped with uncertain execution ownership",
            "Inspect repository state before choosing an explicit resolution",
            "Retrying may duplicate an operation whose outcome is uncertain",
        )

    def _transition_human_required(
        self,
        state: ProjectState,
        task: Task,
        *,
        event_types: tuple[str, ...],
        metadata: dict[str, str],
        category: HumanActionCategory,
        summary: str,
        requested_action: str,
        risk: str,
    ) -> ProjectState:
        operation_time = self._clock()
        new_state = request_human_action(
            state,
            category=category,
            summary=summary,
            requested_action=requested_action,
            risk=risk,
            task_id=task.id,
            operation_time=operation_time,
            action_id=f"action-{self._event_id_factory()}",
            event_id_factory=self._event_id_factory,
            source_event_types=event_types,
            source_metadata=metadata,
        )
        self._store.save(new_state)
        return new_state

    def _event(
        self,
        state: ProjectState,
        task: Task,
        event_type: str,
        timestamp: datetime,
        metadata: dict[str, str],
    ) -> ProjectEvent:
        return ProjectEvent(
            id=self._event_id_factory(),
            project_id=state.project.id,
            event_type=event_type,
            entity_id=task.id,
            timestamp=timestamp,
            metadata=metadata,
        )

    @staticmethod
    def _replace_task(tasks: tuple[Task, ...], updated: Task) -> tuple[Task, ...]:
        return tuple(updated if task.id == updated.id else task for task in tasks)

    def _reload_task_state(self, task_id: str) -> tuple[ProjectState, Task]:
        latest = self._store.load()
        return latest, self._task(latest, task_id)

    @staticmethod
    def _human_outcome(
        task_id: str,
        reports: tuple[ExecutionReport, ...],
        decisions: tuple[Decision, ...],
        *,
        final_prompt: str | None,
    ) -> TaskCycleOutcome:
        return TaskCycleOutcome(
            task_id=task_id,
            attempts=len(reports),
            final_decision=SupervisorDecisionType.HUMAN_REQUIRED,
            execution_reports=reports,
            decisions=decisions,
            final_prompt=final_prompt,
            human_action_required=True,
        )


__all__ = [
    "ProjectStateStore",
    "ReviewService",
    "TaskCycleService",
    "WorkerSession",
]
