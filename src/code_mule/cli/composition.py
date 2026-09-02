"""Production composition root for Boss CLI commands."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Protocol, TextIO
from uuid import uuid4

from openai import DefaultHttpx2Client, OpenAI

from code_mule.domain.enums import ChangeRequestStatus, ProjectStatus, TaskStatus
from code_mule.domain.models import Project
from code_mule.orchestrator import (
    ChangeCommand,
    InvalidBossCommand,
    OrchestratorService,
    PauseCommand,
    QueryCommand,
    ResumeCommand,
)
from code_mule.planning import ProjectPlanningRequest, ProjectPlanningService
from code_mule.progress import ConsoleProgressRenderer
from code_mule.replanning import (
    ChangeExecutionService,
    ChangeReplanningRequest,
    ChangeReplanningService,
)
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
    InvalidCliProjectState,
)


class _PlanningService(Protocol):
    def plan(self, request: ProjectPlanningRequest): ...


class _ExecutionService(Protocol):
    def run(self): ...


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

    def init_project(
        self, project_id: str, name: str, workspace: Path
    ) -> CliCommandResult:
        if self._store.exists():
            raise InvalidCliProjectState(
                f"state file already exists: {self._state_file}"
            )
        resolved_workspace = workspace.expanduser().resolve()
        self._validate_workspace_path(resolved_workspace)
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._store.save(_empty_state(project_id, name, resolved_workspace))
        return CliCommandResult(
            CliExitCode.SUCCESS,
            (
                f"project_id: {project_id}",
                "status: idle",
                f"workspace: {resolved_workspace}",
                f"state_file: {self._state_file}",
            ),
        )

    def run(self, objective: str | None) -> CliCommandResult:
        state = self._load()
        status = state.project.status
        if status is ProjectStatus.IDLE:
            if objective in (None, ""):
                raise InvalidCliProjectState(
                    "IDLE project requires run --objective"
                )
            runtime = self._runtime(state)
            with runtime.renderer:
                planning = runtime.planning.plan(
                    ProjectPlanningRequest(state.project.id, objective)
                )
                outcome = runtime.execution.run() if planning.ready_for_execution else None
            final = self._load()
            return self._execution_result(final, outcome, plan_id=planning.plan_id)
        if status is ProjectStatus.RUNNING:
            if objective is not None:
                raise InvalidCliProjectState(
                    "--objective is only valid for an IDLE project"
                )
            runtime = self._runtime(state)
            with runtime.renderer:
                outcome = runtime.execution.run()
            return self._execution_result(self._load(), outcome)
        if status is ProjectStatus.CHANGE_REQUESTED:
            raise InvalidCliProjectState(
                "change is pending; run 'code-mule change --apply' at the Safe Point"
            )
        if status is ProjectStatus.HUMAN_REQUIRED:
            raise CliHumanActionRequired("project requires human action")
        raise InvalidCliProjectState(f"run is not allowed from {status.value}")

    def status(self) -> CliCommandResult:
        state = self._load()
        return CliCommandResult(CliExitCode.SUCCESS, self._status_lines(state))

    def ask(self, question: str) -> CliCommandResult:
        if question == "":
            raise InvalidCliProjectState("question must not be empty")
        state = self._load()
        view = self._orchestrator().query(QueryCommand(state.project.id))
        return CliCommandResult(
            CliExitCode.SUCCESS,
            (f"question: {question}",) + self._status_lines(state, view=view),
        )

    def change(self, request: str) -> CliCommandResult:
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
        return CliCommandResult(
            CliExitCode.SUCCESS,
            (
                f"change_request_id: {result.change_request_id}",
                f"status: {result.current_status.value}",
                "next: code-mule change --apply",
            ),
        )

    def apply_change(self) -> CliCommandResult:
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
        runtime = self._runtime(state)
        with runtime.renderer:
            outcome = runtime.change_execution.apply_and_resume(
                ChangeReplanningRequest(state.project.id, pending[0].id)
            )
        final = self._load()
        lines = (
            f"replacement_plan_id: {outcome.replanning.plan_id}",
            f"replacement_plan_version: {outcome.replanning.plan_version}",
        ) + self._status_lines(final)
        code = (
            CliExitCode.HUMAN_ACTION_REQUIRED
            if outcome.human_action_required
            else CliExitCode.SUCCESS
        )
        return CliCommandResult(code, lines)

    def pause(self) -> CliCommandResult:
        state = self._load()
        try:
            result = self._orchestrator().pause(PauseCommand(state.project.id))
        except InvalidBossCommand as error:
            raise InvalidCliProjectState("pause is invalid for current state") from error
        return CliCommandResult(CliExitCode.SUCCESS, (result.message,))

    def resume(self) -> CliCommandResult:
        state = self._load()
        if state.project.status is ProjectStatus.HUMAN_REQUIRED:
            raise CliHumanActionRequired(
                "resume cannot bypass HUMAN_REQUIRED"
            )
        self._validate_resume_state(state)
        try:
            result = self._orchestrator().resume(ResumeCommand(state.project.id))
        except InvalidBossCommand as error:
            raise InvalidCliProjectState("resume is invalid for current state") from error
        return CliCommandResult(CliExitCode.SUCCESS, (result.message,))

    def _runtime(self, state: ProjectState) -> RuntimeComposition:
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
        supervisor = SupervisorService(
            DeepSeekSupervisorModelClient(
                OpenAI(
                    api_key=api_key,
                    base_url="https://api.deepseek.com",
                    max_retries=0,
                    http_client=DefaultHttpx2Client(trust_env=False),
                ),
                DeepSeekSupervisorConfig(model=model, max_output_tokens=None),
            )
        )
        return self._compose_runtime(state, supervisor)

    def _compose_runtime(
        self, state: ProjectState, supervisor: SupervisorService
    ) -> RuntimeComposition:
        workspace = self._workspace(state)
        clock = lambda: datetime.now(UTC)
        renderer = ConsoleProgressRenderer(self._stderr)
        worker_config = CodexWorkerConfig(
            command=("codex", "app-server"),
            workspace=workspace,
            approval_policy="on-request",
            sandbox="workspace-write",
            read_timeout_seconds=360,
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

    def _status_lines(self, state: ProjectState, *, view=None) -> tuple[str, ...]:
        if view is None:
            view = self._orchestrator().query(QueryCommand(state.project.id))
        active_plan = next(
            (plan for plan in state.plans if plan.id == state.project.active_plan_id),
            None,
        )
        blockers = tuple(
            task.id for task in state.tasks if task.status is TaskStatus.BLOCKED
        )
        return (
            f"project_id: {view.project_id}",
            f"project_status: {view.status.value}",
            f"active_plan: {view.active_plan_id or '-'}",
            f"active_plan_version: {active_plan.version if active_plan else '-'}",
            f"task_progress: {view.completed_tasks}/{view.total_tasks}",
            f"current_task: {view.current_task_id or '-'}",
            f"blockers: {','.join(blockers) if blockers else '-'}",
            "human_action_required: "
            + str(view.status is ProjectStatus.HUMAN_REQUIRED).lower(),
        )

    def _execution_result(self, state: ProjectState, outcome, *, plan_id=None):
        lines = (() if plan_id is None else (f"plan_id: {plan_id}",)) + self._status_lines(state)
        if outcome is not None:
            lines += (f"execution_stop_reason: {outcome.stop_reason.value}",)
        code = (
            CliExitCode.HUMAN_ACTION_REQUIRED
            if state.project.status is ProjectStatus.HUMAN_REQUIRED
            else CliExitCode.SUCCESS
        )
        return CliCommandResult(code, lines)


__all__ = ["ProductionCliComposition", "RuntimeComposition", "RuntimeFactory"]
