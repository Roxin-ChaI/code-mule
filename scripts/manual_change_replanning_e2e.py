"""Boss-only real DeepSeek + Codex CHANGE replanning E2E."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for path in (_ROOT, _SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from openai import DefaultHttpx2Client, OpenAI  # noqa: E402

from code_mule.orchestrator import ChangeCommand, OrchestratorService  # noqa: E402
from code_mule.planning import ProjectPlanningRequest, ProjectPlanningService  # noqa: E402
from code_mule.progress import ConsoleProgressRenderer  # noqa: E402
from code_mule.replanning import (  # noqa: E402
    ChangeExecutionService,
    ChangeReplanningRequest,
    ChangeReplanningService,
)
from code_mule.runtime import (  # noqa: E402
    ProjectExecutionConfig,
    ProjectExecutionStopReason,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import (  # noqa: E402
    TaskScheduler,
    resolve_active_plan_graph,
)
from code_mule.state.models import ProjectState  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor.providers.deepseek import (  # noqa: E402
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService  # noqa: E402
from code_mule.worker import CodexWorkerConfig, CodexWorkerSession  # noqa: E402
from scripts.local_codex_autonomous_smoke import (  # noqa: E402
    _IdFactory,
    _empty_state,
)
from scripts.local_codex_change_replanning_smoke import (  # noqa: E402
    OBJECTIVE,
    _ChangeAfterTaskCycle,
)


MANUAL_CHANGE = "Also add multiply support and unit tests."


class _ObservedReplanningService:
    def __init__(self, service, store) -> None:
        self._service = service
        self._store = store
        self.materialized_state: ProjectState | None = None

    def replan(self, request):
        outcome = self._service.replan(request)
        self.materialized_state = self._store.load()
        return outcome


def _replacement_plan_diagnostics(state: ProjectState) -> dict[str, object]:
    scheduler = TaskScheduler()
    scheduler.validate(state)
    graph = resolve_active_plan_graph(state)
    requirement_ids = set(graph.plan.requirement_ids)
    traceability_valid = all(
        set(task.requirement_ids) <= requirement_ids for task in graph.tasks
    )
    plan_complete = scheduler.is_plan_complete(state)
    ready = None if plan_complete else scheduler.select_next(state)
    return {
        "replacement_project_status": state.project.status.value,
        "replacement_active_plan_id": state.project.active_plan_id,
        "replacement_current_task_id": state.project.current_task_id,
        "replacement_plan_status": graph.plan.status.value,
        "replacement_plan_active": graph.plan.status.value == "active",
        "replacement_plan_version": graph.plan.version,
        "replacement_task_ids": [task.id for task in graph.tasks],
        "replacement_task_statuses": {
            task.id: task.status.value for task in graph.tasks
        },
        "replacement_tasks": [
            {
                "id": task.id,
                "status": task.status.value,
                "dependencies": list(task.dependencies),
                "requirement_ids": list(task.requirement_ids),
            }
            for task in graph.tasks
        ],
        "dependencies_valid": True,
        "traceability_valid": traceability_valid,
        "plan_complete_before_resume": plan_complete,
        "ready_task_id_before_resume": None if ready is None else ready.id,
        "ready_task_available": plan_complete or ready is not None,
    }


def _human_required_diagnostics(execution, state: ProjectState) -> dict[str, object]:
    if execution is None:
        return {
            "human_required_reason": "replanning_not_ready",
            "human_required_task_id": state.project.current_task_id,
            "human_required_attempt": None,
            "human_required_final_decision": None,
            "human_required_failure_category": "replanning",
            "human_required_error_type": None,
        }
    if not execution.human_action_required:
        return {
            "human_required_reason": None,
            "human_required_task_id": None,
            "human_required_attempt": None,
            "human_required_final_decision": None,
            "human_required_failure_category": None,
            "human_required_error_type": None,
        }

    task_id = state.project.current_task_id
    if task_id is None and execution.task_ids:
        task_id = execution.task_ids[-1]
    reports = tuple(
        report for report in state.execution_reports if report.task_id == task_id
    )
    decisions = tuple(
        decision for decision in state.decisions if decision.task_id == task_id
    )
    attempt = None if not reports else reports[-1].attempt
    final_decision = None if not decisions else decisions[-1].type.value
    reason = execution.stop_reason.value
    category = reason
    error_type = None
    if execution.stop_reason is ProjectExecutionStopReason.HUMAN_REQUIRED:
        reason = "human_required_unspecified"
        category = "task_cycle"
        for event in reversed(state.events):
            if (
                event.event_type.startswith("task.")
                and task_id is not None
                and event.entity_id != task_id
            ):
                continue
            if event.event_type == "task.cycle_limit_reached":
                reason = "task_cycle_limit"
                category = "task_cycle_limit"
                break
            if event.event_type == "task.human_required":
                source = event.metadata.get("source")
                if source in {"worker_report", "supervisor"}:
                    reason = source
                    category = source
                elif "error_type" in event.metadata:
                    reason = "worker_failure"
                    category = "worker_failure"
                    error_type = event.metadata["error_type"]
                else:
                    reason = "task_human_required"
                    continue
                break
            if event.event_type == "project.execution_recovery_required":
                reason = "execution_recovery_required"
                category = "project_execution"
                break
            if event.event_type == "project.task_cycle_stopped":
                reason = "task_cycle_stopped"
                category = "task_cycle"
                break
    return {
        "human_required_reason": reason,
        "human_required_task_id": task_id,
        "human_required_attempt": attempt,
        "human_required_final_decision": final_decision,
        "human_required_failure_category": category,
        "human_required_error_type": error_type,
    }


def _build_compatibility_client(deepseek_api_key: str) -> OpenAI:
    return OpenAI(
        api_key=deepseek_api_key,
        base_url="https://api.deepseek.com",
        max_retries=0,
        http_client=DefaultHttpx2Client(trust_env=False),
    )


def _build_supervisor_config(model: str) -> DeepSeekSupervisorConfig:
    return DeepSeekSupervisorConfig(model=model, max_output_tokens=None)


def _run(supervisor: SupervisorService) -> int:
    with TemporaryDirectory(prefix="code-mule-manual-change-") as temporary:
        root = Path(temporary)
        workspace = root / "repository"
        workspace.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        store = JsonProjectStateStore(root / "project-state.json")
        store.save(_empty_state(datetime.now(UTC)))
        clock = lambda: datetime.now(UTC)
        renderer = ConsoleProgressRenderer()
        planning = ProjectPlanningService(
            store=store,
            supervisor=supervisor,
            clock=clock,
            plan_id_factory=_IdFactory("manual-plan"),
            event_id_factory=_IdFactory("manual-planning-event"),
            progress_sink=renderer,
        )
        orchestrator = OrchestratorService(
            store,
            clock=clock,
            event_id_factory=_IdFactory("manual-boss-event"),
            progress_sink=renderer,
        )
        injected = False

        def inject_change(task_id: str) -> None:
            nonlocal injected
            if injected:
                return
            orchestrator.change(
                ChangeCommand(
                    "autonomous-local",
                    MANUAL_CHANGE,
                    "boss",
                    "manual-change-1",
                )
            )
            injected = True

        report_ids = _IdFactory("manual-change-report")
        decision_ids = _IdFactory("manual-change-decision")
        cycle_event_ids = _IdFactory("manual-change-cycle-event")

        def task_cycle_factory():
            def worker_session_factory() -> CodexWorkerSession:
                return CodexWorkerSession(
                    CodexWorkerConfig(
                        command=("codex", "app-server"),
                        workspace=workspace,
                        approval_policy="on-request",
                        sandbox="workspace-write",
                        read_timeout_seconds=360,
                    ),
                    progress_sink=renderer,
                    clock=clock,
                )

            cycle = TaskCycleService(
                worker_session_factory=worker_session_factory,
                supervisor=supervisor,
                store=store,
                clock=clock,
                report_id_factory=report_ids,
                decision_id_factory=decision_ids,
                event_id_factory=cycle_event_ids,
                config=TaskCycleConfig(max_attempts=2),
                progress_sink=renderer,
            )
            return _ChangeAfterTaskCycle(cycle, inject_change)

        execution = ProjectExecutionService(
            store=store,
            scheduler=TaskScheduler(),
            task_cycle_factory=task_cycle_factory,
            prompt_builder=TaskPromptBuilder(),
            clock=clock,
            event_id_factory=_IdFactory("manual-execution-event"),
            config=ProjectExecutionConfig(max_tasks_per_run=20),
            progress_sink=renderer,
        )
        replanning = ChangeReplanningService(
            store=store,
            supervisor=supervisor,
            clock=clock,
            plan_id_factory=_IdFactory("manual-replan"),
            event_id_factory=_IdFactory("manual-replanning-event"),
            progress_sink=renderer,
        )
        observed_replanning = _ObservedReplanningService(replanning, store)
        changed_execution = ChangeExecutionService(
            replanning_service=observed_replanning,
            execution_service=execution,
        )

        with renderer:
            planning_outcome = planning.plan(
                ProjectPlanningRequest("autonomous-local", OBJECTIVE)
            )
            safe_point = execution.run()
            changed = changed_execution.apply_and_resume(
                ChangeReplanningRequest("autonomous-local", "manual-change-1")
            )
        final = store.load()
        replacement_state = observed_replanning.materialized_state
        if replacement_state is None:
            raise RuntimeError("replacement Plan state was not observed")
        replacement = _replacement_plan_diagnostics(replacement_state)
        resumed = changed.execution
        resumed_task_ids = () if resumed is None else resumed.task_ids
        diagnostics = {
            "objective": OBJECTIVE,
            "change": MANUAL_CHANGE,
            "initial_plan_id": planning_outcome.plan_id,
            "safe_point_task_ids": list(safe_point.task_ids),
            "replacement_plan_id": changed.replanning.plan_id,
            **replacement,
            "replacement_plan_version_matches_outcome": (
                replacement["replacement_plan_version"]
                == changed.replanning.plan_version
            ),
            "tasks_started": None if resumed is None else resumed.tasks_started,
            "tasks_completed": None if resumed is None else resumed.tasks_completed,
            "execution_stop_reason": (
                None if resumed is None else resumed.stop_reason.value
            ),
            "final_project_status": final.project.status.value,
            "human_action_required": changed.human_action_required,
            "current_task_id": final.project.current_task_id,
            "completed_safe_point_task_redispatched": bool(
                set(safe_point.task_ids) & set(resumed_task_ids)
            ),
            **_human_required_diagnostics(resumed, final),
        }
        print(json.dumps(diagnostics, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    print("REAL DEEPSEEK + CODEX CHANGE REPLANNING — MANUAL ONLY")
    print("This makes real DeepSeek PLAN, Impact Analysis, and REVIEW requests.")
    print("It may incur billing and uses real locally authenticated Codex.")
    print("All Worker changes occur in a disposable temporary Git repository.")
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    model = os.getenv("CODE_MULE_DEEPSEEK_MODEL")
    if not deepseek_api_key:
        print("ERROR: DEEPSEEK_API_KEY is required.", file=sys.stderr)
        return 2
    if not model:
        print("ERROR: CODE_MULE_DEEPSEEK_MODEL is required.", file=sys.stderr)
        return 2
    supervisor = SupervisorService(
        DeepSeekSupervisorModelClient(
            _build_compatibility_client(deepseek_api_key),
            _build_supervisor_config(model),
        )
    )
    return _run(supervisor)


if __name__ == "__main__":
    raise SystemExit(main())
