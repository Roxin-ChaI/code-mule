"""Deterministic control flow for one current task and one Worker thread."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import (
    HumanActionCategory,
    HumanActionStatus,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
    WorkerHumanActionKind,
)
from code_mule.domain.models import (
    Decision,
    ExecutionReport,
    ProjectEvent,
    Task,
    WorkerCapabilityApprovalDetails,
    WorkerInputDetails,
)
from code_mule.domain.state_machine import validate_transition
from code_mule.human import request_human_action
from code_mule.human import pending_action
from code_mule.git_delivery import (
    GitBaseline,
    GitChangeSet,
    GitCommitResult,
    GitDeliveryMode,
    GitNoCommitResult,
    GitDeliveryError,
    WorkerVerificationError,
    GitOwnershipError,
)
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.state.models import ProjectState
from code_mule.recovery import (
    BoundaryRecoverability,
    ExecutionAttemptStatus,
    ExecutionPhase,
    ExecutionStopReason,
    SafePointKind,
    WorkerTerminalState,
)
from code_mule.recovery.state import (
    start_attempt,
    update_attempt,
    with_safe_point,
    with_stop_boundary,
)
from code_mule.supervisor.contracts import ReviewRequest, ReviewResult
from code_mule.supervisor import supervisor_failure_metadata
from code_mule.worker.contracts import (
    CodexApprovalRequired,
    CodexCapabilityApprovalRequired,
    CodexUserInputRequired,
    CodexTurnTimeout,
    CodexWorkerError,
    WorkerTaskRequest,
    worker_failure_metadata,
)
from code_mule.transport import TransportDiagnostics

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

    @property
    def turn_id(self) -> str | None: ...

    def start(self) -> None: ...

    def execute(
        self,
        request: WorkerTaskRequest,
        *,
        report_id: str,
        created_at: datetime,
    ) -> ExecutionReport: ...

    def transport_diagnostics(self) -> TransportDiagnostics | None: ...

    def close(self) -> None: ...


class ReviewService(Protocol):
    def review(self, request: ReviewRequest) -> ReviewResult: ...


class GitDelivery(Protocol):
    def capture_baseline(self, task_id: str) -> GitBaseline: ...

    def capture_partial_paths(self, baseline: GitBaseline) -> tuple[str, ...]: ...

    def prepare_change_set(
        self,
        baseline: GitBaseline,
        report: ExecutionReport,
        owned_paths: tuple[str, ...],
    ) -> GitChangeSet: ...

    def commit(self, change_set: GitChangeSet, task: Task) -> GitCommitResult: ...

    def verify_no_commit(
        self, change_set: GitChangeSet, task: Task
    ) -> GitNoCommitResult: ...


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
        git_delivery: GitDelivery | None = None,
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
        self._git_delivery = git_delivery

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def execute(self, request: TaskCycleRequest) -> TaskCycleOutcome:
        state = self._store.load()
        task = self._validate_start(state, request)
        reports: tuple[ExecutionReport, ...] = ()
        decisions: tuple[Decision, ...] = ()
        prompt = request.initial_prompt
        baseline: GitBaseline | None = None
        continuation_partial_paths: tuple[str, ...] = ()
        continuation_requested = self._has_worker_input_answer(state, task)

        if state.project.status is ProjectStatus.CANCEL_REQUESTED:
            self._cancel_current_task(task.id)
            return self._cancelled_outcome(task.id, reports, decisions)

        if self._git_delivery is not None:
            try:
                continuation = self._worker_input_continuation(state, task)
                if continuation is None:
                    baseline = self._git_delivery.capture_baseline(task.id)
                    state = self._persist_git_baseline(state, task, baseline)
                    self._emit_progress(
                        state,
                        task,
                        ProgressEventType.GIT_BASELINE_CAPTURED,
                        "Clean Git baseline captured",
                    )
                else:
                    details, baseline = continuation
                    current_paths = self._git_delivery.capture_partial_paths(baseline)
                    if current_paths != details.partial_paths:
                        raise GitOwnershipError(
                            "workspace changed after the Worker input request"
                        )
                    continuation_partial_paths = details.partial_paths
                    prompt = self._continuation_prompt(prompt, details)
            except GitDeliveryError as error:
                if continuation_requested:
                    self._record_git_failure(state, task, error, "continuation")
                else:
                    self._record_workspace_block(state, task, error)
                self._emit_git_failure(state, task, error, "baseline", attempt=1)
                return self._human_outcome(
                    task.id, reports, decisions, final_prompt=None
                )

        session = self._worker_session_factory()

        try:
            latest = self._store.load()
            if latest.project.status is ProjectStatus.CANCEL_REQUESTED:
                self._cancel_current_task(task.id)
                return self._cancelled_outcome(task.id, reports, decisions)
            attempt = max((item.attempt for item in latest.execution_attempts if item.task_id == task.id), default=0) + 1
            # Persist uncertainty before any external session startup. A crash in
            # start() must never look like a Task that has not dispatched.
            state = self._record_execution_started(latest, task, attempt, baseline, None)
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
                self._record_worker_failure(
                    state, task, error, transport=self._session_transport(session)
                )
                self._emit_worker_failure(state, task, error)
                return self._human_outcome(
                    task.id, reports, decisions, final_prompt=None
                )
            first_attempt = True
            while True:
                task = self._task(state, request.task.id)
                if first_attempt:
                    first_attempt = False
                    state = update_attempt(
                        self._store.load(),
                        task.id,
                        attempt,
                        ExecutionAttemptStatus.WORKER_STARTED,
                        thread_id=getattr(session, "thread_id", None),
                        transport=self._session_transport(session),
                    )
                    self._store.save(state)
                else:
                    attempt = max((item.attempt for item in state.execution_attempts if item.task_id == task.id), default=0) + 1
                    state = self._record_execution_started(state, task, attempt, baseline, getattr(session, "thread_id", None))
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
                    try:
                        state = self._record_worker_failure(
                            state,
                            task,
                            error,
                            baseline,
                            transport=self._session_transport(session),
                        )
                    except GitDeliveryError as git_error:
                        state = self._record_git_failure(
                            self._store.load(), task, git_error, "worker_input"
                        )
                        self._emit_git_failure(
                            state,
                            task,
                            git_error,
                            "worker_input",
                            attempt=task.execution_attempts + 1,
                        )
                        self._clear_worker_identity(task.id)
                        return self._human_outcome(
                            task.id, reports, decisions, final_prompt=None
                        )
                    if isinstance(
                        error,
                        (CodexUserInputRequired, CodexCapabilityApprovalRequired),
                    ):
                        self._clear_worker_identity(task.id)
                    self._emit_worker_failure(state, task, error)
                    return self._human_outcome(
                        task.id,
                        reports,
                        decisions,
                        final_prompt=None,
                    )

                state = self._store.load()
                state = update_attempt(
                    state,
                    task.id,
                    attempt,
                    ExecutionAttemptStatus.WORKER_COMPLETED,
                    turn_id=getattr(session, "turn_id", None),
                    terminal_at=self._clock(),
                    transport=self._session_transport(session),
                )
                self._store.save(state)

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

                if report.human_action is not None:
                    try:
                        state = self._record_report_human_action(
                            state, self._task(state, task.id), report, baseline
                        )
                    except GitDeliveryError as error:
                        state = self._record_git_failure(
                            self._store.load(), task, error, "worker_report"
                        )
                        self._emit_git_failure(
                            state, task, error, "worker_report", attempt=report.attempt
                        )
                        self._clear_worker_identity(task.id)
                        return self._human_outcome(
                            task.id, reports, decisions, final_prompt=None
                        )
                    self._clear_worker_identity(task.id)
                    self._emit_human_gate(
                        state,
                        self._task(state, task.id),
                        "worker_report",
                        attempt=report.attempt,
                    )
                    return self._human_outcome(
                        task.id, reports, decisions, final_prompt=None
                    )

                change_set: GitChangeSet | None = None
                if self._git_delivery is not None:
                    if baseline is None:
                        raise InvalidTaskCycleState("Git delivery requires a baseline")
                    if not set(continuation_partial_paths).issubset(
                        report.files_changed
                    ):
                        error = GitOwnershipError(
                            "continued Worker report omitted partial baseline paths"
                        )
                        self._record_git_failure(state, task, error, "ownership")
                        self._emit_git_failure(
                            state,
                            task,
                            error,
                            "ownership",
                            attempt=report.attempt,
                        )
                        self._clear_worker_identity(task.id)
                        return self._human_outcome(
                            task.id, reports, decisions, final_prompt=None
                        )
                    owned_paths = tuple(
                        dict.fromkeys(
                            continuation_partial_paths
                            + tuple(
                                path
                                for persisted_report in reports
                                for path in persisted_report.files_changed
                            )
                        )
                    )
                    try:
                        change_set = self._git_delivery.prepare_change_set(
                            baseline,
                            report,
                            owned_paths,
                        )
                        self._emit_progress(
                            state,
                            task,
                            ProgressEventType.GIT_CHANGE_SET_VERIFIED,
                            "Task Git ownership verified",
                            attempt=report.attempt,
                            metadata={"path_count": str(len(change_set.changed_paths))},
                        )
                    except GitDeliveryError as error:
                        stage = "verification" if isinstance(error, WorkerVerificationError) else "ownership"
                        self._record_git_failure(state, task, error, stage)
                        self._emit_git_failure(
                            state,
                            task,
                            error,
                            stage,
                            attempt=report.attempt,
                        )
                        self._clear_worker_identity(task.id)
                        return self._human_outcome(
                            task.id, reports, decisions, final_prompt=None
                        )

                persisted = self._store.load()
                persisted_task = self._task(persisted, task.id)
                persisted = update_attempt(
                    persisted,
                    task.id,
                    attempt,
                    ExecutionAttemptStatus.REVIEW_STARTED,
                    terminal_at=self._latest_attempt(persisted, task.id).terminal_at,
                )
                self._store.save(persisted)
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
                    failure_metadata = supervisor_failure_metadata(error)
                    self._emit_progress(
                        persisted,
                        persisted_task,
                        ProgressEventType.SUPERVISOR_FAILED,
                        "Supervisor review failed",
                        attempt=report.attempt,
                        metadata=failure_metadata,
                    )
                    self._transition_human_required(
                        persisted,
                        persisted_task,
                        event_types=("supervisor.review_failed", "task.human_required"),
                        metadata=failure_metadata,
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
                state = update_attempt(
                    state,
                    task.id,
                    attempt,
                    ExecutionAttemptStatus.REVIEW_COMPLETED,
                    terminal_at=self._latest_attempt(state, task.id).terminal_at,
                )
                self._store.save(state)
                decisions += (decision,)

                if review.decision is SupervisorDecisionType.CONTINUE:
                    if change_set is not None:
                        if not self._deliver_task(change_set, persisted_task, attempt):
                            self._clear_worker_identity(task.id)
                            return self._human_outcome(
                                task.id, reports, decisions, final_prompt=None
                            )
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
                    if change_set is not None:
                        if not self._deliver_task(change_set, persisted_task, attempt):
                            self._clear_worker_identity(task.id)
                            return self._human_outcome(
                                task.id, reports, decisions, final_prompt=None
                            )
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
                if self._store.load().project.status is ProjectStatus.CANCEL_REQUESTED:
                    self._cancel_current_task(task.id)
                    self._clear_worker_identity(task.id)
                    return self._cancelled_outcome(task.id, reports, decisions)
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
            # Cleanup ordering: the typed failure boundary is already persisted
            # above, so refine only the bounded transport facts that cleanup
            # itself produced (for example the child's real exit code).
            self._refresh_attempt_transport(
                request.task.id, self._session_transport(session)
            )

    @staticmethod
    def _session_transport(session: WorkerSession) -> TransportDiagnostics | None:
        """Read a session's bounded transport snapshot without ever raising."""

        reader = getattr(session, "transport_diagnostics", None)
        if not callable(reader):
            return None
        try:
            snapshot = reader()
        except Exception:
            return None
        return snapshot if isinstance(snapshot, TransportDiagnostics) else None

    def _refresh_attempt_transport(
        self, task_id: str, transport: TransportDiagnostics | None
    ) -> None:
        """Refine the latest attempt's transport evidence after cleanup."""

        if transport is None:
            return
        try:
            state = self._store.load()
            attempts = tuple(
                item for item in state.execution_attempts if item.task_id == task_id
            )
            if not attempts:
                return
            latest = max(attempts, key=lambda item: item.attempt)
            if latest.transport is None or latest.transport == transport:
                return
            refreshed = update_attempt(
                state,
                task_id,
                latest.attempt,
                latest.status,
                thread_id=latest.thread_id,
                turn_id=latest.turn_id,
                baseline_head=latest.baseline_head,
                terminal_at=latest.terminal_at,
                failure_kind=latest.failure_kind,
                partial_paths_exist=latest.partial_paths_exist,
                transport=transport,
            )
            self._store.save(refreshed)
        except Exception:
            # Evidence refinement must never mask the original Worker outcome.
            return

    def resume_after_report(self, request: TaskCycleRequest) -> TaskCycleOutcome:
        """Continue a trusted persisted report without creating a Worker."""

        state = self._store.load()
        task = self._validate_start(state, request)
        attempt = self._latest_attempt(state, task.id)
        if attempt.status is not ExecutionAttemptStatus.REPORT_PERSISTED:
            raise InvalidTaskCycleState("recovery requires a report-persisted boundary")
        reports = tuple(
            item for item in state.execution_reports
            if item.task_id == task.id and item.attempt == task.execution_attempts
        )
        if len(reports) != 1:
            raise InvalidTaskCycleState("trusted Worker report must exist exactly once")
        report = reports[0]
        if report.human_action is not None:
            raise InvalidTaskCycleState("report recovery cannot bypass a Worker Human Gate")
        baselines = tuple(
            item for item in state.git_baselines
            if item.task_id == task.id and item.baseline_head == attempt.baseline_head
        )
        if self._git_delivery is None or len(baselines) != 1:
            raise InvalidTaskCycleState("report recovery requires its original Git baseline")
        change_set = self._git_delivery.prepare_change_set(
            baselines[0], report, report.files_changed
        )
        state = update_attempt(
            state, task.id, attempt.attempt, ExecutionAttemptStatus.REVIEW_STARTED,
            terminal_at=attempt.terminal_at,
        )
        self._store.save(state)
        try:
            review = self._supervisor.review(ReviewRequest(state, task, report))
        except BaseException as error:
            self._transition_human_required(
                self._store.load(), task,
                event_types=("supervisor.review_failed", "task.human_required"),
                metadata=supervisor_failure_metadata(error),
                category=HumanActionCategory.SUPERVISOR_FAILURE,
                summary="Supervisor review failed",
                requested_action="Inspect the failure and choose an explicit resolution",
                risk="Execution cannot continue without a trustworthy Supervisor decision",
            )
            raise
        state, decision = self._persist_decision(self._store.load(), task, review)
        state = update_attempt(
            state, task.id, attempt.attempt, ExecutionAttemptStatus.REVIEW_COMPLETED,
            terminal_at=attempt.terminal_at,
        )
        self._store.save(state)
        if review.decision not in {
            SupervisorDecisionType.CONTINUE, SupervisorDecisionType.DONE
        }:
            self._transition_human_required(
                state,
                task,
                event_types=("task.recovery_review_requires_human", "task.human_required"),
                metadata={"decision": review.decision.value},
                category=HumanActionCategory.SUPERVISOR_FAILURE,
                summary="Recovered report requires further human-guided work",
                requested_action="Inspect the review decision and choose an explicit resolution",
                risk="A Worker will not be restarted automatically after recovery review",
            )
            return self._human_outcome(task.id, (report,), (decision,), final_prompt=None)
        if not self._deliver_task(change_set, task, attempt.attempt):
            return self._human_outcome(task.id, (report,), (decision,), final_prompt=None)
        self._complete_task(self._store.load(), task, review.decision)
        self._clear_worker_identity(task.id)
        return TaskCycleOutcome(
            task_id=task.id,
            attempts=0,
            final_decision=review.decision,
            execution_reports=(report,),
            decisions=(decision,),
            final_prompt=None,
            human_action_required=False,
        )

    def _clear_worker_identity(self, task_id: str) -> None:
        if self._worker_identity_cleared is not None:
            self._worker_identity_cleared(task_id)

    def _emit_worker_failure(
        self, state: ProjectState, task: Task, error: CodexWorkerError
    ) -> None:
        lifecycle = tuple(item for item in state.execution_attempts if item.task_id == task.id)
        attempt = max(lifecycle, key=lambda item: item.attempt).attempt if lifecycle else task.execution_attempts + 1
        self._emit_progress(
            state,
            task,
            ProgressEventType.WORKER_FAILED,
            "Codex Worker failed",
            attempt=attempt,
            metadata=worker_failure_metadata(error),
        )
        self._emit_human_gate(
            state, task, "worker_failure", attempt=attempt
        )

    def _emit_git_failure(
        self,
        state: ProjectState,
        task: Task,
        error: GitDeliveryError,
        stage: str,
        *,
        attempt: int,
    ) -> None:
        self._emit_progress(
            state,
            task,
            ProgressEventType.GIT_DELIVERY_FAILED,
            ("Worker verification requires human action" if isinstance(error, WorkerVerificationError)
             else "Task Git delivery requires human action"),
            attempt=attempt,
            metadata={"error_type": type(error).__name__, "stage": stage},
        )
        self._emit_human_gate(
            state, task,
            "worker_verification" if isinstance(error, WorkerVerificationError) else "git_delivery",
            attempt=attempt,
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
        if state.project.status not in {
            ProjectStatus.RUNNING,
            ProjectStatus.CANCEL_REQUESTED,
        }:
            raise InvalidTaskCycleState("project must be RUNNING or CANCEL_REQUESTED")
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
        self,
        state: ProjectState,
        task: Task,
        attempt: int,
        baseline: GitBaseline | None,
        thread_id: str | None,
    ) -> ProjectState:
        state, task = self._reload_task_state(task.id)
        operation_time = self._clock()
        event = self._event(
            state,
            task,
            "task.execution_started",
            operation_time,
            {"attempt": str(attempt)},
        )
        new_state = replace(
            state,
            project=replace(state.project, updated_at=operation_time),
            events=state.events + (event,),
        )
        new_state = start_attempt(
            new_state,
            task_id=task.id,
            attempt=attempt,
            recorded_at=operation_time,
            baseline_head=None if baseline is None else baseline.baseline_head,
        )
        self._store.save(new_state)
        new_state = update_attempt(
            self._store.load(),
            task.id,
            attempt,
            ExecutionAttemptStatus.WORKER_STARTED,
            thread_id=thread_id,
        )
        self._store.save(new_state)
        return new_state

    def _persist_git_baseline(
        self, state: ProjectState, task: Task, baseline: GitBaseline
    ) -> ProjectState:
        state, task = self._reload_task_state(task.id)
        if baseline.task_id != task.id:
            raise InvalidTaskCycleState("Git baseline targets a different task")
        if baseline in state.git_baselines:
            return state
        operation_time = self._clock()
        event = self._event(
            state,
            task,
            "git.baseline_captured",
            operation_time,
            {"baseline_head": baseline.baseline_head},
        )
        new_state = replace(
            state,
            project=replace(state.project, updated_at=operation_time),
            git_baselines=state.git_baselines + (baseline,),
            events=state.events + (event,),
        )
        new_state = with_safe_point(
            new_state,
            SafePointKind.TASK_BASELINE_CAPTURED,
            operation_time,
            task_id=task.id,
            attempt=task.execution_attempts + 1,
            head_sha=baseline.baseline_head,
        )
        self._store.save(new_state)
        return new_state

    def _deliver_task(
        self, change_set: GitChangeSet, task: Task, attempt: int
    ) -> bool:
        if self._git_delivery is None:
            raise InvalidTaskCycleState("Git delivery service is unavailable")
        latest = self._store.load()
        latest_task = self._task(latest, task.id)
        try:
            if (
                latest.project.status is ProjectStatus.HUMAN_REQUIRED
                or pending_action(latest) is not None
            ):
                raise GitOwnershipError("pending Human Gate blocks Git commit")
            operation_time = self._clock()
            latest = update_attempt(
                latest,
                task.id,
                attempt,
                ExecutionAttemptStatus.DELIVERY_STARTED,
                terminal_at=self._latest_attempt(latest, task.id).terminal_at,
            )
            change_event = self._event(
                latest,
                latest_task,
                "git.change_set_verified",
                operation_time,
                {"path_count": str(len(change_set.changed_paths))},
            )
            with_change_set = replace(
                latest,
                project=replace(latest.project, updated_at=operation_time),
                git_change_sets=latest.git_change_sets + (change_set,),
                events=latest.events + (change_event,),
            )
            self._store.save(with_change_set)
            if change_set.delivery_mode is GitDeliveryMode.NO_COMMIT_REQUIRED:
                result = self._git_delivery.verify_no_commit(
                    change_set, latest_task
                )
                latest = self._store.load()
                event = self._event(
                    latest,
                    self._task(latest, task.id),
                    "git.no_commit_required",
                    result.verified_at,
                    {
                        "delivery_mode": result.delivery_mode.value,
                        "verified_head": result.verified_head,
                    },
                )
                delivered = replace(
                    latest,
                    project=replace(
                        latest.project, updated_at=result.verified_at
                    ),
                    events=latest.events + (event,),
                )
                delivered = update_attempt(
                    delivered,
                    task.id,
                    attempt,
                    ExecutionAttemptStatus.DELIVERED,
                    terminal_at=result.verified_at,
                )
                delivered = with_safe_point(
                    delivered,
                    SafePointKind.TASK_DELIVERED,
                    result.verified_at,
                    task_id=task.id,
                    attempt=attempt,
                    head_sha=result.verified_head,
                )
                self._store.save(delivered)
                self._emit_progress(
                    delivered,
                    self._task(delivered, task.id),
                    ProgressEventType.GIT_NO_COMMIT_REQUIRED,
                    "Task required no repository commit",
                    attempt=attempt,
                    metadata={"delivery_mode": result.delivery_mode.value},
                )
                return True
            result = self._git_delivery.commit(change_set, latest_task)
            latest = self._store.load()
            commit_event = self._event(
                latest,
                self._task(latest, task.id),
                "git.committed",
                result.committed_at,
                {
                    "commit_sha": result.commit_sha,
                    "path_count": str(len(result.changed_paths)),
                },
            )
            committed = replace(
                latest,
                project=replace(latest.project, updated_at=result.committed_at),
                git_commit_results=latest.git_commit_results + (result,),
                events=latest.events + (commit_event,),
            )
            committed = update_attempt(
                committed,
                task.id,
                attempt,
                ExecutionAttemptStatus.DELIVERED,
                terminal_at=result.committed_at,
            )
            committed = with_safe_point(
                committed,
                SafePointKind.TASK_DELIVERED,
                result.committed_at,
                task_id=task.id,
                attempt=attempt,
                head_sha=result.commit_sha,
            )
            self._store.save(committed)
            self._emit_progress(
                committed,
                self._task(committed, task.id),
                ProgressEventType.GIT_COMMITTED,
                "Task delivered in a local Git commit",
                attempt=attempt,
                metadata={"commit_sha": result.commit_sha},
            )
            return True
        except GitDeliveryError as error:
            failed_state = self._store.load()
            failed_task = self._task(failed_state, task.id)
            self._record_git_failure(failed_state, failed_task, error, "commit")
            self._emit_git_failure(
                failed_state, failed_task, error, "commit", attempt=attempt
            )
            return False

    def _record_git_failure(
        self,
        state: ProjectState,
        task: Task,
        error: GitDeliveryError,
        stage: str,
    ) -> ProjectState:
        verification = isinstance(error, WorkerVerificationError)
        metadata = {"error_type": type(error).__name__, "stage": stage}
        if verification and error.check is not None:
            from code_mule.domain.worker_verification import safe_check_name

            metadata.update({
                "check_name": safe_check_name(error.check.name),
                "check_type": error.check.check_type.value,
                "check_status": error.check.status.value,
                "check_required": str(error.check.required).lower(),
            })
        return self._transition_human_required(
            state,
            task,
            event_types=("git.delivery_failed", "task.human_required"),
            metadata=metadata,
            category=(HumanActionCategory.WORKER_VERIFICATION if verification
                      else HumanActionCategory.RECOVERY_UNCERTAIN),
            summary=("Worker verification blocked delivery" if verification
                     else "Task Git delivery could not be completed safely"),
            requested_action=("Complete the required verification before delivery" if verification
                              else "Inspect repository ownership and choose an explicit resolution"),
            risk=("Delivering without required verification would bypass Task quality gates" if verification
                  else "Committing may include unrelated work or duplicate an uncertain delivery"),
        )

    def _record_workspace_block(
        self,
        state: ProjectState,
        task: Task,
        error: GitDeliveryError,
    ) -> ProjectState:
        return self._transition_human_required(
            state,
            task,
            event_types=("git.baseline_failed", "task.human_required"),
            metadata={"error_type": type(error).__name__, "stage": "baseline"},
            category=HumanActionCategory.WORKSPACE_BLOCK,
            summary="Task workspace precondition was not satisfied",
            requested_action=(
                "Restore a clean Git workspace, then resolve this action with retry_task"
            ),
            risk="No Worker was started; unrelated workspace changes must be preserved",
        )

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
        new_state = update_attempt(
            new_state,
            task.id,
            self._latest_attempt(new_state, task.id).attempt,
            ExecutionAttemptStatus.REPORT_PERSISTED,
            terminal_at=self._latest_attempt(new_state, task.id).terminal_at,
        )
        new_state = with_safe_point(
            new_state,
            SafePointKind.TASK_WORKER_COMPLETED,
            report.created_at,
            task_id=task.id,
            attempt=self._latest_attempt(new_state, task.id).attempt,
            head_sha=self._latest_attempt(new_state, task.id).baseline_head,
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
        self,
        state: ProjectState,
        task: Task,
        error: CodexWorkerError,
        baseline: GitBaseline | None = None,
        transport: TransportDiagnostics | None = None,
    ) -> ProjectState:
        category, summary, requested_action, risk = self._worker_failure_action(error)
        worker_input = None
        capability_approval = None
        latest = self._store.load()
        lifecycle = tuple(item for item in latest.execution_attempts if item.task_id == task.id)
        attempt = max(lifecycle, key=lambda item: item.attempt).attempt if lifecycle else task.execution_attempts + 1
        partial_paths: tuple[str, ...] = ()
        baseline_head = None
        if baseline is not None and self._git_delivery is not None:
            partial_paths = self._git_delivery.capture_partial_paths(baseline)
            baseline_head = baseline.baseline_head
        if isinstance(error, CodexCapabilityApprovalRequired):
            request = error.request
            capability_approval = WorkerCapabilityApprovalDetails(
                request_method=request.method,
                request_id=request.request_id,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
                server_name=request.server_name,
                capability=request.capability,
                application=request.application,
                capability_id=request.capability_id,
                tool_name=request.tool_name,
                approval_scopes=request.available_scopes,
                worker_attempt=attempt,
                baseline_head=baseline_head,
                partial_paths=partial_paths,
                native_request_active=False,
            )
            repeated = any(
                action.task_id == task.id
                and action.capability_approval is not None
                and action.capability_approval.identity == capability_approval.identity
                and action.status is not HumanActionStatus.PENDING
                for action in latest.human_actions
            )
            if repeated:
                category = HumanActionCategory.RECOVERY_UNCERTAIN
                summary = "Permission continuation failed after a prior decision"
                requested_action = (
                    "Inspect the preserved workspace and choose an explicit safe resolution"
                )
                risk = (
                    "A fresh Worker requested the same native capability; another "
                    "automatic continuation could repeat indefinitely"
                )
        elif isinstance(error, CodexUserInputRequired):
            request = error.request
            worker_input = WorkerInputDetails(
                request_method=request.method,
                request_id=request.request_id,
                question=request.question,
                choices=request.choices,
                worker_attempt=attempt,
                baseline_head=baseline_head,
                partial_paths=partial_paths,
            )
        if any(item.task_id == task.id and item.attempt == attempt for item in latest.execution_attempts):
            latest = update_attempt(
                latest,
                task.id,
                attempt,
                ExecutionAttemptStatus.UNCERTAIN,
                terminal_at=self._clock(),
                failure_kind=type(error).__name__.lower()[:64],
                partial_paths_exist=bool(partial_paths),
                transport=transport,
            )
            self._store.save(latest)
        safe_partial_paths = tuple(
            path[:240]
            for path in partial_paths[:100]
            if "\n" not in path and "\x00" not in path
        )
        partial_metadata = {"partial_path_count": str(len(safe_partial_paths))}
        partial_metadata.update(
            {
                f"partial_path_{index}": path
                for index, path in enumerate(safe_partial_paths, start=1)
            }
        )
        transitioned = self._transition_human_required(
            latest,
            task,
            event_types=("task.execution_failed", "task.human_required"),
            metadata={
                **worker_failure_metadata(error),
                **partial_metadata,
                **(
                    {"failure_kind": "permission_continuation_loop"}
                    if category is HumanActionCategory.RECOVERY_UNCERTAIN
                    and capability_approval is not None
                    else {}
                ),
            },
            category=category,
            summary=summary,
            requested_action=requested_action,
            risk=risk,
            worker_input=worker_input,
            capability_approval=capability_approval,
        )
        if category is HumanActionCategory.RECOVERY_UNCERTAIN:
            reason = (
                ExecutionStopReason.WORKER_TIMEOUT
                if isinstance(error, CodexTurnTimeout)
                else ExecutionStopReason.WORKER_FAILED
            )
            transitioned = with_stop_boundary(
                transitioned,
                reason=reason,
                phase=ExecutionPhase.WORKER,
                safe_point=SafePointKind.HUMAN_GATE,
                recoverability=BoundaryRecoverability.UNCERTAIN,
                worker_started=bool(lifecycle),
                worker_terminal_state=(
                    WorkerTerminalState.TIMEOUT
                    if isinstance(error, CodexTurnTimeout)
                    else WorkerTerminalState.FAILED
                ) if lifecycle else WorkerTerminalState.NOT_STARTED,
                report_persisted=False,
                recorded_at=self._clock(),
                task_id=task.id,
                attempt=attempt,
                head_sha=None if baseline is None else baseline.baseline_head,
            )
            self._store.save(transitioned)
        return transitioned

    def _record_report_human_action(
        self,
        state: ProjectState,
        task: Task,
        report: ExecutionReport,
        baseline: GitBaseline | None,
    ) -> ProjectState:
        action = report.human_action
        if action is None:
            raise InvalidTaskCycleState("Worker report has no human action")
        partial_paths: tuple[str, ...] = ()
        baseline_head = None
        if self._git_delivery is not None:
            if baseline is None:
                raise GitOwnershipError("Worker human action lacks its Git baseline")
            partial_paths = self._git_delivery.capture_partial_paths(baseline)
            baseline_head = baseline.baseline_head
            if not set(partial_paths).issubset(report.files_changed):
                raise GitOwnershipError("Worker human action omitted partial paths")
        category = {
            WorkerHumanActionKind.INPUT: HumanActionCategory.WORKER_INPUT,
            WorkerHumanActionKind.APPROVAL: HumanActionCategory.WORKER_APPROVAL,
            WorkerHumanActionKind.EXTERNAL_SIDE_EFFECT: HumanActionCategory.EXTERNAL_SIDE_EFFECT,
        }[action.kind]
        details = None
        if action.kind is WorkerHumanActionKind.INPUT:
            details = WorkerInputDetails(
                request_method="worker/report",
                request_id=report.id,
                question=action.request,
                choices=action.choices,
                worker_attempt=report.attempt,
                baseline_head=baseline_head,
                partial_paths=partial_paths,
            )
        return self._transition_human_required(
            state,
            task,
            event_types=("task.human_required",),
            metadata={
                "source": "worker_report",
                "kind": action.kind.value,
                "attempt": str(report.attempt),
                "report_id": report.id,
            },
            category=category,
            summary=action.summary,
            requested_action=action.request,
            risk=(
                "Partial work is preserved; answering requires an explicit run in a fresh Worker session"
                if action.kind is WorkerHumanActionKind.INPUT
                else action.summary
            ),
            worker_input=details,
        )

    def _worker_input_continuation(
        self, state: ProjectState, task: Task
    ) -> tuple[WorkerInputDetails, GitBaseline] | None:
        actions = tuple(
            action
            for action in state.human_actions
            if action.task_id == task.id
            and action.category is HumanActionCategory.WORKER_INPUT
            and action.status is HumanActionStatus.RESOLVED
            and action.worker_input is not None
            and action.worker_input.answer is not None
            and self._input_attempt_matches(state, task, action.worker_input)
        )
        if not actions:
            return None
        if len(actions) != 1:
            raise GitOwnershipError(
                "Worker input continuation is ambiguous"
            )
        details = actions[-1].worker_input
        if details is None or details.baseline_head is None:
            raise GitOwnershipError(
                "Worker input continuation lacks its original Git baseline"
            )
        baselines = tuple(
            item
            for item in state.git_baselines
            if item.task_id == task.id
            and item.baseline_head == details.baseline_head
        )
        if len(baselines) != 1:
            raise GitOwnershipError(
                "Worker input continuation baseline is unavailable or ambiguous"
            )
        return details, baselines[0]

    @staticmethod
    def _has_worker_input_answer(state: ProjectState, task: Task) -> bool:
        return any(
            action.task_id == task.id
            and action.category is HumanActionCategory.WORKER_INPUT
            and action.status is HumanActionStatus.RESOLVED
            and action.worker_input is not None
            and action.worker_input.answer is not None
            and TaskCycleService._input_attempt_matches(state, task, action.worker_input)
            for action in state.human_actions
        )

    @staticmethod
    def _input_attempt_matches(
        state: ProjectState, task: Task, details: WorkerInputDetails
    ) -> bool:
        if details.request_method != "worker/report":
            return details.worker_attempt == task.execution_attempts + 1
        # Report-level input has already incremented the persisted attempt
        # count. Bind continuation to that exact typed report, once only.
        return details.worker_attempt == task.execution_attempts and any(
            report.id == details.request_id
            and report.task_id == task.id
            and report.attempt == details.worker_attempt
            and report.human_action is not None
            and report.human_action.kind is WorkerHumanActionKind.INPUT
            for report in state.execution_reports
        )

    @staticmethod
    def _continuation_prompt(
        original_prompt: str, details: WorkerInputDetails
    ) -> str:
        choices = (
            "\nAvailable choices from the original request: "
            + "; ".join(details.choices)
            if details.choices
            else ""
        )
        return (
            f"{original_prompt}\n\n"
            "Continue the same Task in a fresh Worker session. The previous session "
            "may have left correct partial workspace changes; inspect the workspace "
            "first and do not repeat or destroy completed work.\n"
            f"Worker question: {details.question}{choices}\n"
            f"Boss answer: {details.answer}\n"
            "Report every changed path relative to the original clean Git baseline."
        )

    @staticmethod
    def _worker_failure_action(error: CodexWorkerError):
        if isinstance(error, CodexCapabilityApprovalRequired):
            return (
                HumanActionCategory.WORKER_APPROVAL,
                "Codex Worker requires native capability approval",
                error.request.request,
                "This capability can only continue through its original live app-server request",
            )
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
                "Answer this specific Worker input request",
                "Worker may already have changed the workspace; the original session cannot be resumed automatically",
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
        worker_input: WorkerInputDetails | None = None,
        capability_approval: WorkerCapabilityApprovalDetails | None = None,
    ) -> ProjectState:
        state, task = self._reload_task_state(task.id)
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
            worker_input=worker_input,
            capability_approval=capability_approval,
        )
        self._store.save(new_state)
        return new_state

    def _cancel_current_task(self, task_id: str) -> ProjectState:
        state, task = self._reload_task_state(task_id)
        if state.project.status is not ProjectStatus.CANCEL_REQUESTED:
            raise InvalidTaskCycleState("Task cancellation requires CANCEL_REQUESTED")
        if state.project.current_task_id != task.id:
            raise InvalidTaskCycleState("Task cancellation requires the current Task")
        if task.status is not TaskStatus.IN_PROGRESS:
            raise InvalidTaskCycleState("Task cancellation requires IN_PROGRESS")
        now = self._clock()
        cancelled = replace(task, status=TaskStatus.CANCELLED, updated_at=now)
        event = self._event(
            state,
            task,
            "task.cancelled",
            now,
            {"reason": "project_cancellation"},
        )
        updated = replace(
            state,
            project=replace(state.project, current_task_id=None, updated_at=now),
            tasks=self._replace_task(state.tasks, cancelled),
            events=state.events + (event,),
        )
        self._store.save(updated)
        return updated

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
    def _latest_attempt(state: ProjectState, task_id: str):
        attempts = tuple(item for item in state.execution_attempts if item.task_id == task_id)
        if not attempts:
            raise InvalidTaskCycleState("Task execution attempt is unavailable")
        return max(attempts, key=lambda item: item.attempt)

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

    @staticmethod
    def _cancelled_outcome(
        task_id: str,
        reports: tuple[ExecutionReport, ...],
        decisions: tuple[Decision, ...],
    ) -> TaskCycleOutcome:
        return TaskCycleOutcome(
            task_id=task_id,
            attempts=len(reports),
            final_decision=SupervisorDecisionType.CONTINUE,
            execution_reports=reports,
            decisions=decisions,
            final_prompt=None,
            human_action_required=False,
            cancelled=True,
        )


__all__ = [
    "ProjectStateStore",
    "GitDelivery",
    "ReviewService",
    "TaskCycleService",
    "WorkerSession",
]
