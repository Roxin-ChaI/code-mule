"""Production composition root for Boss CLI commands."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Protocol, TextIO
from uuid import uuid4

from openai import DefaultHttpx2Client, OpenAI

from code_mule.domain.enums import (
    ChangeRequestStatus,
    HumanResolutionStrategy,
    ProjectStatus,
)
from code_mule.domain.models import Project, ProjectEvent
from code_mule.diagnosis import ProjectDiagnosisService
from code_mule.conversation import (
    BossConversationService,
    BossSession,
    CompositeBossIntentRouter,
    DeterministicBossIntentRouter,
    StructuredBossIntentRouter,
    run_chat_loop,
)
from code_mule.human import (
    HumanResolutionError,
    HumanResolutionService,
    pending_action,
)
from code_mule.git_delivery import (
    GitDeliveryService,
    GitWorkspaceIsolationError,
    register_state_exclusion,
)
from code_mule.execution import (
    ExecutionAlreadyOwned,
    ExecutionRecoveryRequired,
)
from code_mule.execution.service import (
    ExecutionOwnershipHandle,
    ExecutionOwnershipService,
)
from code_mule.orchestrator import (
    ChangeCommand,
    InvalidBossCommand,
    OrchestratorService,
    PauseCommand,
    QueryCommand,
    ResumeCommand,
    StopCommand,
)
from code_mule.planning import ProjectPlanningRequest, ProjectPlanningService
from code_mule.progress import ConsoleProgressRenderer
from code_mule.project_verification.service import (
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.presentation import (
    render_change_applied,
    render_change_requested,
    render_human_action,
    render_project,
    render_project_diagnosis,
    render_project_cancelled,
    render_project_cancellation_requested,
    status_label,
)
from code_mule.replanning import (
    ChangeExecutionService,
    ChangeReplanningRequest,
    ChangeReplanningService,
)
from code_mule.recovery import (
    RecoveryMode,
    SafePointKind,
    SafePoint,
)
from code_mule.recovery.service import RecoveryClassifier, RecoveryPreflightError
from code_mule.recovery.state import with_safe_point
from code_mule.runtime import (
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler
from code_mule.state.models import ProjectState
from code_mule.state.serialization import InvalidProjectState
from code_mule.state.store import JsonProjectStateStore, ProjectStateNotFound
from code_mule.supervisor.providers.deepseek import (
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService
from code_mule.worker import (
    CodexWorkerConfig,
    CodexWorkerService,
    CodexWorkerSession,
)

from .contracts import (
    CliCommandResult,
    CliExecutionFailure,
    CliExitCode,
    CliHumanActionRequired,
    CliProjectAlreadyRunning,
    CliRecoveryRequired,
    InvalidCliProjectState,
)


class _PlanningService(Protocol):
    def plan(self, request: ProjectPlanningRequest): ...


class _ExecutionService(Protocol):
    def run(self): ...
    def recover(self, plan): ...


class _ChangeService(Protocol):
    def apply_and_resume(self, request: ChangeReplanningRequest): ...


@dataclass(frozen=True)
class RuntimeComposition:
    supervisor: object
    worker_service: object
    planning: _PlanningService
    execution: _ExecutionService
    change_execution: _ChangeService
    renderer: ConsoleProgressRenderer


RuntimeFactory = Callable[[ProjectState], RuntimeComposition]


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def _empty_state(project_id: str, name: str, workspace: Path) -> ProjectState:
    now = datetime.now(UTC)
    return ProjectState(
        project=Project(
            project_id,
            name,
            ProjectStatus.IDLE,
            None,
            None,
            now,
            now,
            str(workspace),
        ),
        requirements=(),
        plans=(),
        milestones=(),
        tasks=(),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
        latest_safe_point=SafePoint(SafePointKind.PROJECT_IDLE, now),
    )


class ProductionCliComposition:
    """Bind persisted state to existing deterministic services on demand."""

    def __init__(
        self,
        state_file: Path,
        *,
        environment: Mapping[str, str] | None = None,
        stdout: TextIO,
        stderr: TextIO,
        runtime_factory: RuntimeFactory | None = None,
    ) -> None:
        self._state_file = state_file.expanduser().resolve()
        self._environment = os.environ if environment is None else environment
        self._stdout = stdout
        self._stderr = stderr
        self._store = JsonProjectStateStore(self._state_file)
        self._runtime_factory = runtime_factory
        self._diagnosis_service = ProjectDiagnosisService()

    def init_project(
        self, project_id: str, name: str, workspace: Path, verbose: bool = False
    ) -> CliCommandResult:
        if self._store.exists():
            raise InvalidCliProjectState(
                f"state file already exists: {self._state_file}"
            )
        resolved_workspace = workspace.expanduser().resolve()
        self._validate_workspace_path(resolved_workspace)
        try:
            register_state_exclusion(resolved_workspace, self._state_file)
        except GitWorkspaceIsolationError as error:
            raise InvalidCliProjectState(
                "Code Mule state could not be isolated from the Git workspace"
            ) from error
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        state = _empty_state(project_id, name, resolved_workspace)
        self._store.save(state)
        lines = render_project(state, verbose=verbose, heading="PROJECT INITIALIZED")
        lines += ("", f"Workspace   {resolved_workspace}",)
        if verbose:
            lines += (f"state_file: {self._state_file}",)
        return CliCommandResult(
            CliExitCode.SUCCESS,
            lines,
        )

    def run(self, objective: str | None, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        status = state.project.status
        if status is ProjectStatus.IDLE:
            if objective in (None, ""):
                raise InvalidCliProjectState(
                    "IDLE project requires run --objective"
                )
            try:
                with self._acquire_execution(verbose) as ownership:
                    runtime = self._runtime(self._load(), ownership)
                    with runtime.renderer:
                        planning = runtime.planning.plan(
                            ProjectPlanningRequest(state.project.id, objective)
                        )
                        ownership.heartbeat()
                        outcome = (
                            runtime.execution.run()
                            if planning.ready_for_execution
                            else None
                        )
            except KeyboardInterrupt:
                self._raise_interrupted()
            final = self._load()
            return self._execution_result(final, outcome, verbose=verbose)
        if status is ProjectStatus.RUNNING:
            if objective is not None:
                raise InvalidCliProjectState(
                    "--objective is only valid for an IDLE project"
                )
            try:
                with self._acquire_execution(verbose) as ownership:
                    runtime = self._runtime(self._load(), ownership)
                    with runtime.renderer:
                        outcome = runtime.execution.run()
            except KeyboardInterrupt:
                self._raise_interrupted()
            return self._execution_result(self._load(), outcome, verbose=verbose)
        if status is ProjectStatus.CHANGE_REQUESTED:
            raise InvalidCliProjectState(
                "change is pending; run 'code-mule change --apply' at the Safe Point"
            )
        if status is ProjectStatus.HUMAN_REQUIRED:
            raise CliHumanActionRequired("project requires human action")
        raise InvalidCliProjectState(f"run is not allowed from {status.value}")

    def status(self, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        return CliCommandResult(
            CliExitCode.SUCCESS, render_project(state, verbose=verbose)
        )

    def diagnose(self, verbose: bool = False) -> CliCommandResult:
        diagnosis = self._diagnosis_service.diagnose(self._load())
        return CliCommandResult(
            CliExitCode.SUCCESS,
            render_project_diagnosis(diagnosis, verbose=verbose),
        )

    def ask(self, question: str, verbose: bool = False) -> CliCommandResult:
        if question == "":
            raise InvalidCliProjectState("question must not be empty")
        state = self._load()
        self._orchestrator().query(QueryCommand(state.project.id))
        return CliCommandResult(
            CliExitCode.SUCCESS,
            ("PROJECT QUERY", f"Question    {question}", "")
            + render_project(state, verbose=verbose, heading="ANSWER"),
        )

    def change(self, request: str, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        try:
            result = self._orchestrator().change(
                ChangeCommand(
                    state.project.id,
                    request,
                    "boss",
                    _id("change"),
                )
            )
        except InvalidBossCommand as error:
            raise InvalidCliProjectState("change is invalid for current state") from error
        lines = render_change_requested(self._load(), request, verbose=verbose)
        if verbose:
            lines += (f"change_request_id: {result.change_request_id}",)
        return CliCommandResult(CliExitCode.SUCCESS, lines)

    def apply_change(self, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        if state.project.status is not ProjectStatus.CHANGE_REQUESTED:
            raise InvalidCliProjectState(
                "change --apply requires CHANGE_REQUESTED"
            )
        if state.project.current_task_id is not None:
            raise InvalidCliProjectState(
                "change --apply requires a Task Safe Point"
            )
        pending = tuple(
            item
            for item in state.change_requests
            if item.status is ChangeRequestStatus.PENDING
        )
        if len(pending) != 1:
            raise InvalidCliProjectState(
                "change --apply requires exactly one pending ChangeRequest"
            )
        try:
            with self._acquire_execution(verbose) as ownership:
                runtime = self._runtime(self._load(), ownership)
                with runtime.renderer:
                    outcome = runtime.change_execution.apply_and_resume(
                        ChangeReplanningRequest(state.project.id, pending[0].id)
                    )
        except KeyboardInterrupt:
            self._raise_interrupted()
        final = self._load()
        stop_reason = getattr(getattr(outcome, "execution", None), "stop_reason", None)
        stop_value = None if stop_reason is None else stop_reason.value
        lines = render_change_applied(
            state,
            final,
            verbose=verbose,
            execution_stop_reason=stop_value,
        )
        if verbose:
            lines += (
                f"replacement_plan_id: {outcome.replanning.plan_id}",
                f"replacement_plan_version: {outcome.replanning.plan_version}",
            )
        code = (
            CliExitCode.HUMAN_ACTION_REQUIRED
            if outcome.human_action_required
            else CliExitCode.SUCCESS
        )
        return CliCommandResult(code, lines)

    def pause(self, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        try:
            result = self._orchestrator().pause(PauseCommand(state.project.id))
        except InvalidBossCommand as error:
            raise InvalidCliProjectState("pause is invalid for current state") from error
        return CliCommandResult(
            CliExitCode.SUCCESS,
            ("PROJECT PAUSED", result.message, "")
            + render_project(self._load(), verbose=verbose),
        )

    def resume(self, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        if state.project.status is ProjectStatus.HUMAN_REQUIRED:
            raise CliHumanActionRequired(
                "resume cannot bypass HUMAN_REQUIRED"
            )
        self._validate_resume_state(state)
        try:
            with self._acquire_execution(verbose):
                result = self._orchestrator().resume(
                    ResumeCommand(state.project.id)
                )
        except InvalidBossCommand as error:
            raise InvalidCliProjectState("resume is invalid for current state") from error
        return CliCommandResult(
            CliExitCode.SUCCESS,
            ("EXECUTION RESUMED", result.message, "")
            + render_project(self._load(), verbose=verbose),
        )

    def recover(self, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        try:
            plan = RecoveryClassifier().classify(state, validate_workspace=True)
        except RecoveryPreflightError as error:
            raise CliRecoveryRequired(str(error)) from error
        if not plan.automatic_resume_allowed:
            raise CliRecoveryRequired(plan.reason)
        try:
            with self._acquire_execution(verbose) as ownership:
                if plan.recovery_mode is RecoveryMode.FRESH_PLANNING:
                    objective = state.project.objective
                    if objective in (None, ""):
                        raise CliRecoveryRequired(
                            "planning recovery requires the persisted objective"
                        )
                    now = datetime.now(UTC)
                    reset = replace(
                        state,
                        project=replace(
                            state.project,
                            status=ProjectStatus.IDLE,
                            active_plan_id=None,
                            current_task_id=None,
                            updated_at=now,
                        ),
                        latest_execution_stop=None,
                        events=state.events
                        + (
                            ProjectEvent(
                                _id("recovery-event"),
                                state.project.id,
                                "planning.recovery_started",
                                state.project.id,
                                now,
                                {"mode": plan.recovery_mode.value},
                            ),
                        ),
                    )
                    reset = with_safe_point(
                        reset, SafePointKind.PROJECT_IDLE, now
                    )
                    self._store.save(reset)
                    runtime = self._runtime(reset, ownership)
                    with runtime.renderer:
                        planned = runtime.planning.plan(
                            ProjectPlanningRequest(reset.project.id, objective)
                        )
                        outcome = (
                            runtime.execution.run()
                            if planned.ready_for_execution
                            else None
                        )
                else:
                    runtime = self._runtime(state, ownership)
                    with runtime.renderer:
                        outcome = runtime.execution.recover(plan)
        except KeyboardInterrupt:
            self._raise_interrupted()
        final = self._load()
        lines = (
            "RECOVERY",
            f"From           {plan.from_safe_point.value.replace('_', ' ').title()}",
            f"Mode           {plan.recovery_mode.value.replace('_', ' ').title()}",
            f"Fresh Worker   {'Yes' if plan.fresh_worker_required else 'No'}",
            "",
        )
        result = self._execution_result(final, outcome, verbose=verbose)
        return CliCommandResult(result.exit_code, lines + result.output)

    def stop(self, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        try:
            result = self._orchestrator().stop(StopCommand(state.project.id))
        except InvalidBossCommand as error:
            raise InvalidCliProjectState("stop is invalid for current state") from error
        latest = self._load()
        if result.current_status is ProjectStatus.CANCEL_REQUESTED:
            lines = render_project_cancellation_requested(latest, verbose=verbose)
        else:
            lines = render_project_cancelled(latest, verbose=verbose)
        return CliCommandResult(CliExitCode.SUCCESS, lines)

    def inspect(self, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        try:
            action = pending_action(state)
        except ValueError as error:
            raise InvalidCliProjectState("human action state is ambiguous") from error
        if action is None:
            raise InvalidCliProjectState("no pending HumanAction")
        return CliCommandResult(
            CliExitCode.SUCCESS, render_human_action(action, verbose=verbose, state=state)
        )

    def approve(self, action_id: str, verbose: bool = False) -> CliCommandResult:
        try:
            state = self._human_resolution().approve(action_id)
        except HumanResolutionError as error:
            raise InvalidCliProjectState(str(error)) from error
        action = self._action(state, action_id)
        lines = (
            "ACTION APPROVED",
            "The approval is bound to this action only.",
            "No operation has been executed.",
            "Worker session recovery is not available; the project remains safely stopped.",
        )
        if verbose:
            lines += (
                f"action_id: {action.id}",
                f"action_status: {action.status.value}",
                f"project_status: {state.project.status.value}",
            )
        return CliCommandResult(CliExitCode.SUCCESS, lines)

    def reject(self, action_id: str, verbose: bool = False) -> CliCommandResult:
        try:
            state = self._human_resolution().reject(action_id)
        except HumanResolutionError as error:
            raise InvalidCliProjectState(str(error)) from error
        action = self._action(state, action_id)
        lines = (
            "ACTION REJECTED",
            "The requested operation was not executed.",
            "The project remains safely stopped.",
        )
        if verbose:
            lines += (
                f"action_id: {action.id}",
                f"action_status: {action.status.value}",
                f"project_status: {state.project.status.value}",
            )
        return CliCommandResult(CliExitCode.SUCCESS, lines)

    def answer(
        self, action_id: str, answer: str, verbose: bool = False
    ) -> CliCommandResult:
        try:
            state = self._human_resolution().answer(action_id, answer)
        except HumanResolutionError as error:
            raise InvalidCliProjectState(str(error)) from error
        action = self._action(state, action_id)
        lines = (
            "WORKER INPUT ANSWERED",
            "A fresh Worker will continue this Task on the next explicit run.",
            "Partial workspace changes were preserved.",
            "No Worker was started by this command.",
        )
        if verbose:
            lines += (
                f"action_id: {action.id}",
                f"action_status: {action.status.value}",
                f"project_status: {state.project.status.value}",
                f"task_id: {action.task_id or '-'}",
            )
        return CliCommandResult(CliExitCode.SUCCESS, lines)

    def resolve(
        self,
        action_id: str,
        strategy: HumanResolutionStrategy,
        verbose: bool = False,
    ) -> CliCommandResult:
        try:
            state = self._human_resolution().resolve(action_id, strategy)
        except HumanResolutionError as error:
            raise InvalidCliProjectState(str(error)) from error
        action = self._action(state, action_id)
        lines = (
            "ACTION RESOLVED",
            f"Strategy    {strategy.value.replace('_', ' ').title()}",
            f"Project     {status_label(state.project.status)}",
        )
        if verbose:
            lines += (
                f"action_id: {action.id}",
                f"action_status: {action.status.value}",
                f"project_status: {state.project.status.value}",
            )
        return CliCommandResult(CliExitCode.SUCCESS, lines)

    def chat(self, input_stream: TextIO, verbose: bool = False) -> CliCommandResult:
        state = self._load()
        service = BossConversationService(
            state_loader=self._load,
            commands=self,
            router=self._boss_intent_router(),
            session=BossSession(state.project.id),
            diagnosis_service=self._diagnosis_service,
            verbose=verbose,
        )
        run_chat_loop(
            service,
            input_stream=input_stream,
            output_stream=self._stdout,
        )
        return CliCommandResult(CliExitCode.SUCCESS)

    def _boss_intent_router(self):
        deterministic = DeterministicBossIntentRouter()
        api_key = self._environment.get("DEEPSEEK_API_KEY")
        if not api_key:
            return CompositeBossIntentRouter(deterministic=deterministic)
        model = self._environment.get(
            "CODE_MULE_DEEPSEEK_MODEL", "deepseek-v4-flash"
        )
        client = DeepSeekSupervisorModelClient(
            OpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com",
                max_retries=0,
                http_client=DefaultHttpx2Client(trust_env=False),
            ),
            DeepSeekSupervisorConfig(model=model, max_output_tokens=None),
        )
        return CompositeBossIntentRouter(
            deterministic=deterministic,
            model=StructuredBossIntentRouter(client),
        )

    def _human_resolution(self) -> HumanResolutionService:
        return HumanResolutionService(
            self._store,
            clock=lambda: datetime.now(UTC),
            event_id_factory=lambda: _id("human-event"),
            resolution_id_factory=lambda: _id("resolution"),
        )

    @staticmethod
    def _action(state: ProjectState, action_id: str):
        return next(action for action in state.human_actions if action.id == action_id)

    def _runtime(
        self,
        state: ProjectState,
        ownership: ExecutionOwnershipHandle | None = None,
    ) -> RuntimeComposition:
        self._workspace(state)
        if self._runtime_factory is not None:
            return self._runtime_factory(state)
        api_key = self._environment.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise CliExecutionFailure(
                "DEEPSEEK_API_KEY is required for model-dependent commands"
            )
        model = self._environment.get(
            "CODE_MULE_DEEPSEEK_MODEL", "deepseek-v4-flash"
        )
        clock = lambda: datetime.now(UTC)
        renderer = ConsoleProgressRenderer(self._stderr)
        supervisor = SupervisorService(
            DeepSeekSupervisorModelClient(
                OpenAI(
                    api_key=api_key,
                    base_url="https://api.deepseek.com",
                    max_retries=0,
                    http_client=DefaultHttpx2Client(trust_env=False),
                ),
                DeepSeekSupervisorConfig(model=model, max_output_tokens=None),
            ),
            progress_sink=renderer,
            clock=clock,
        )
        return self._compose_runtime(
            state,
            supervisor,
            ownership,
            renderer=renderer,
        )

    def _compose_runtime(
        self,
        state: ProjectState,
        supervisor: SupervisorService,
        ownership: ExecutionOwnershipHandle | None = None,
        renderer: ConsoleProgressRenderer | None = None,
    ) -> RuntimeComposition:
        workspace = self._workspace(state)
        clock = lambda: datetime.now(UTC)
        renderer = renderer or ConsoleProgressRenderer(self._stderr)
        worker_config = CodexWorkerConfig(
            command=("codex", "app-server"),
            workspace=workspace,
            approval_policy="on-request",
            sandbox="workspace-write",
            inactivity_timeout_seconds=120,
            max_turn_seconds=900,
        )
        worker_service = CodexWorkerService(
            worker_config, progress_sink=renderer, clock=clock
        )

        def task_cycle_factory() -> TaskCycleService:
            return TaskCycleService(
                worker_session_factory=lambda: CodexWorkerSession(
                    worker_config, progress_sink=renderer, clock=clock
                ),
                supervisor=supervisor,
                store=self._store,
                clock=clock,
                report_id_factory=lambda: _id("report"),
                decision_id_factory=lambda: _id("decision"),
                event_id_factory=lambda: _id("task-event"),
                config=TaskCycleConfig(max_attempts=2),
                progress_sink=renderer,
                worker_identity_started=(
                    None
                    if ownership is None
                    else ownership.record_worker_identity
                ),
                worker_identity_cleared=(
                    None
                    if ownership is None
                    else ownership.clear_worker_identity
                ),
                git_delivery=GitDeliveryService(workspace, clock=clock),
            )

        execution = ProjectExecutionService(
            store=self._store,
            scheduler=TaskScheduler(),
            task_cycle_factory=task_cycle_factory,
            prompt_builder=TaskPromptBuilder(),
            clock=clock,
            event_id_factory=lambda: _id("execution-event"),
            config=ProjectExecutionConfig(max_tasks_per_run=50),
            progress_sink=renderer,
            finalizer=ProjectFinalizationService(
                store=self._store,
                verification=ProjectVerificationService(
                    clock=clock,
                    result_id_factory=lambda: _id("verification"),
                ),
                supervisor=supervisor,
                clock=clock,
                event_id_factory=lambda: _id("verification-event"),
                progress_sink=renderer,
            ),
        )
        planning = ProjectPlanningService(
            store=self._store,
            supervisor=supervisor,
            clock=clock,
            plan_id_factory=lambda: _id("plan"),
            event_id_factory=lambda: _id("planning-event"),
            progress_sink=renderer,
        )
        replanning = ChangeReplanningService(
            store=self._store,
            supervisor=supervisor,
            clock=clock,
            plan_id_factory=lambda: _id("plan"),
            event_id_factory=lambda: _id("replanning-event"),
            progress_sink=renderer,
        )
        return RuntimeComposition(
            supervisor=supervisor,
            worker_service=worker_service,
            planning=planning,
            execution=execution,
            change_execution=ChangeExecutionService(
                replanning_service=replanning,
                execution_service=execution,
            ),
            renderer=renderer,
        )

    def _load(self) -> ProjectState:
        try:
            return self._store.load()
        except ProjectStateNotFound as error:
            raise InvalidCliProjectState(
                f"project state file not found: {self._state_file}"
            ) from error
        except InvalidProjectState as error:
            raise InvalidCliProjectState("project state is invalid") from error

    def _ownership(self) -> ExecutionOwnershipService:
        return ExecutionOwnershipService(
            store=self._store,
            lock_path=self._state_file.parent / "execution.lock",
            clock=lambda: datetime.now(UTC),
            lease_id_factory=lambda: _id("lease"),
            owner_id_factory=lambda: _id("owner"),
            event_id_factory=lambda: _id("execution-event"),
        )

    def _acquire_execution(
        self, verbose: bool
    ) -> ExecutionOwnershipHandle:
        try:
            return self._ownership().acquire()
        except ExecutionAlreadyOwned as error:
            lease = error.lease
            state = self._load()
            current = next(
                (
                    task
                    for task in state.tasks
                    if task.id == state.project.current_task_id
                ),
                None,
            )
            lines = [
                "Another execution owner is active.",
                f"Started     {lease.acquired_at.isoformat()}",
                f"Current     {current.title if current is not None else 'Task boundary'}",
                "No Worker was started by this command.",
            ]
            if verbose:
                lines.extend(
                    (
                        f"owner_id: {lease.owner_id}",
                        f"lease_id: {lease.id}",
                        f"pid: {lease.pid}",
                        f"codex_thread_id: {lease.codex_thread_id or '-'}",
                    )
                )
            raise CliProjectAlreadyRunning("\n".join(lines)) from error
        except ExecutionRecoveryRequired as error:
            raise CliRecoveryRequired(
                "Previous execution ended unexpectedly.\n"
                "The current Task cannot be safely assumed complete.\n"
                f"Recovery classification: {error.decision.classification.value}\n"
                "Inspect and resolve the pending Human Action before continuing."
            ) from error

    def _raise_interrupted(self) -> None:
        state = self._load()
        if state.project.status is ProjectStatus.HUMAN_REQUIRED:
            raise CliRecoveryRequired(
                "Execution was interrupted during an active Task.\n"
                "Repository and Worker side effects require human inspection."
            )
        raise InvalidCliProjectState(
            "execution interrupted at a safe boundary; ownership was released"
        )

    def _orchestrator(self) -> OrchestratorService:
        return OrchestratorService(
            self._store,
            clock=lambda: datetime.now(UTC),
            event_id_factory=lambda: _id("boss-event"),
        )

    def _workspace(self, state: ProjectState) -> Path:
        if state.project.workspace is None:
            raise InvalidCliProjectState(
                "project workspace is not recorded; reinitialize or migrate explicitly"
            )
        workspace = Path(state.project.workspace)
        if not workspace.is_absolute():
            raise InvalidCliProjectState("project workspace must be absolute")
        self._validate_workspace_path(workspace)
        return workspace

    @staticmethod
    def _validate_workspace_path(workspace: Path) -> None:
        if not workspace.exists():
            raise InvalidCliProjectState(f"workspace does not exist: {workspace}")
        if not workspace.is_dir():
            raise InvalidCliProjectState(f"workspace is not a directory: {workspace}")

    def _validate_resume_state(self, state: ProjectState) -> None:
        if state.project.status is not ProjectStatus.PAUSED_BY_BOSS:
            raise InvalidCliProjectState("resume requires PAUSED_BY_BOSS")
        self._workspace(state)
        if state.project.current_task_id is not None:
            raise InvalidCliProjectState(
                "resume requires no in-progress Task ownership"
            )
        try:
            TaskScheduler().validate(state)
        except Exception as error:
            raise InvalidCliProjectState(
                "project Plan is not recoverable"
            ) from error

    def _execution_result(
        self, state: ProjectState, outcome, *, verbose: bool = False
    ):
        stop_reason = None if outcome is None else outcome.stop_reason.value
        lines = render_project(
            state,
            verbose=verbose,
            execution_stop_reason=stop_reason,
        )
        if state.project.status is ProjectStatus.HUMAN_REQUIRED:
            lines += ("", "Next", "  code-mule inspect")
        code = (
            CliExitCode.HUMAN_ACTION_REQUIRED
            if state.project.status is ProjectStatus.HUMAN_REQUIRED
            else CliExitCode.SUCCESS
        )
        return CliCommandResult(code, lines)


__all__ = ["ProductionCliComposition", "RuntimeComposition", "RuntimeFactory"]
