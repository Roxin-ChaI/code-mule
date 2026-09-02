"""Real local Codex + fake Supervisor E2E through the formal Boss CLI."""

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for path in (_ROOT, _SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from code_mule.cli import main as cli_main  # noqa: E402
from code_mule.cli.composition import (  # noqa: E402
    ProductionCliComposition,
    RuntimeComposition,
)
from code_mule.domain.enums import (  # noqa: E402
    HumanActionCategory,
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.human import request_human_action  # noqa: E402
from code_mule.orchestrator import (  # noqa: E402
    ChangeCommand,
    OrchestratorService,
)
from code_mule.planning import ProjectPlanningService  # noqa: E402
from code_mule.progress import ConsoleProgressRenderer  # noqa: E402
from code_mule.replanning import (  # noqa: E402
    ChangeExecutionService,
    ChangeReplanningService,
)
from code_mule.runtime import (  # noqa: E402
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.worker import (  # noqa: E402
    CodexWorkerConfig,
    CodexWorkerService,
    CodexWorkerSession,
)
from scripts.local_codex_autonomous_smoke import _IdFactory  # noqa: E402
from scripts.local_codex_change_replanning_smoke import (  # noqa: E402
    CHANGE,
    OBJECTIVE,
    _ChangeAfterTaskCycle,
    _FakeChangeSupervisor,
)


def main() -> int:
    with TemporaryDirectory(prefix="code-mule-cli-local-") as temporary:
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
        state_file = root / "project-state.json"
        supervisor = _FakeChangeSupervisor()
        output = StringIO()
        errors = StringIO()
        injected = False
        sessions: list[CodexWorkerSession] = []
        renderers: list[ConsoleProgressRenderer] = []
        report_ids = _IdFactory("cli-report")
        decision_ids = _IdFactory("cli-decision")
        cycle_event_ids = _IdFactory("cli-cycle-event")

        def runtime_factory(state):
            nonlocal injected
            store = JsonProjectStateStore(state_file)
            clock = lambda: datetime.now(UTC)
            renderer = ConsoleProgressRenderer(errors)
            renderers.append(renderer)
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
            orchestrator = OrchestratorService(
                store,
                clock=clock,
                event_id_factory=_IdFactory("cli-boss-event"),
                progress_sink=renderer,
            )

            def inject_change(task_id: str) -> None:
                nonlocal injected
                if injected or task_id != "create-calculator":
                    return
                orchestrator.change(
                    ChangeCommand(
                        state.project.id,
                        CHANGE,
                        "boss",
                        "CHANGE-MULTIPLY",
                    )
                )
                injected = True

            def task_cycle_factory():
                def worker_session_factory():
                    session = CodexWorkerSession(
                        worker_config,
                        progress_sink=renderer,
                        clock=clock,
                    )
                    sessions.append(session)
                    return session

                cycle = TaskCycleService(
                    worker_session_factory=worker_session_factory,
                    supervisor=supervisor,
                    store=store,
                    clock=clock,
                    report_id_factory=report_ids,
                    decision_id_factory=decision_ids,
                    event_id_factory=cycle_event_ids,
                    config=TaskCycleConfig(max_attempts=1),
                    progress_sink=renderer,
                )
                return _ChangeAfterTaskCycle(cycle, inject_change)

            execution = ProjectExecutionService(
                store=store,
                scheduler=TaskScheduler(),
                task_cycle_factory=task_cycle_factory,
                prompt_builder=TaskPromptBuilder(),
                clock=clock,
                event_id_factory=_IdFactory("cli-execution-event"),
                config=ProjectExecutionConfig(max_tasks_per_run=10),
                progress_sink=renderer,
            )
            planning = ProjectPlanningService(
                store=store,
                supervisor=supervisor,
                clock=clock,
                plan_id_factory=lambda: "PLAN-1",
                event_id_factory=_IdFactory("cli-planning-event"),
                progress_sink=renderer,
            )
            replanning = ChangeReplanningService(
                store=store,
                supervisor=supervisor,
                clock=clock,
                plan_id_factory=lambda: "PLAN-2",
                event_id_factory=_IdFactory("cli-replanning-event"),
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

        def composition_factory(path, environment, stdout, stderr):
            return ProductionCliComposition(
                path,
                environment=environment,
                stdout=stdout,
                stderr=stderr,
                runtime_factory=runtime_factory,
            )

        common = ["--state-file", str(state_file)]
        init_code = cli_main(
            [
                "init",
                "--project-id",
                "cli-local",
                "--name",
                "CLI local calculator",
                "--workspace",
                str(workspace),
                *common,
            ],
            composition_factory=composition_factory,
            environment={},
            stdout=output,
            stderr=errors,
        )
        run_code = cli_main(
            ["run", "--objective", OBJECTIVE, *common],
            composition_factory=composition_factory,
            environment={},
            stdout=output,
            stderr=errors,
        )
        safe_point = JsonProjectStateStore(state_file).load()
        apply_code = cli_main(
            ["change", "--apply", *common],
            composition_factory=composition_factory,
            environment={},
            stdout=output,
            stderr=errors,
        )
        status_code = cli_main(
            ["status", *common],
            composition_factory=composition_factory,
            environment={},
            stdout=output,
            stderr=errors,
        )
        default_output = output.getvalue()
        verbose_start = len(default_output)
        verbose_status_code = cli_main(
            ["status", "--verbose", *common],
            composition_factory=composition_factory,
            environment={},
            stdout=output,
            stderr=errors,
        )
        verbose_output = output.getvalue()[verbose_start:]
        final = JsonProjectStateStore(state_file).load()

        human_state_file = root / "human-project-state.json"
        human_base = replace(
            final,
            project=replace(
                final.project,
                status=ProjectStatus.RUNNING,
                current_task_id=None,
            ),
            plans=tuple(
                replace(plan, status=PlanStatus.ACTIVE)
                if plan.id == final.project.active_plan_id
                else plan
                for plan in final.plans
            ),
            human_actions=(),
            human_resolutions=(),
        )
        action_ids = _IdFactory("cli-human-event")
        human_state = request_human_action(
            human_base,
            category=HumanActionCategory.EXTERNAL_SIDE_EFFECT,
            summary="Fake gated remote mutation",
            requested_action="Review the fake push request",
            risk="Remote repository mutation",
            task_id=None,
            operation_time=datetime.now(UTC),
            action_id="ACTION-LOCAL-1",
            event_id_factory=action_ids,
            source_event_types=("task.human_required",),
        )
        JsonProjectStateStore(human_state_file).save(human_state)
        inspect_start = len(output.getvalue())
        inspect_code = cli_main(
            ["inspect", "--state-file", str(human_state_file)],
            composition_factory=composition_factory,
            environment={},
            stdout=output,
            stderr=errors,
        )
        inspect_output = output.getvalue()[inspect_start:]
        verification = subprocess.run(
            [sys.executable, "-m", "unittest", "-v"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        payload = {
            "exit_codes": [
                init_code,
                run_code,
                apply_code,
                status_code,
                verbose_status_code,
                inspect_code,
            ],
            "safe_point_status": safe_point.project.status.value,
            "safe_point_current_task": safe_point.project.current_task_id,
            "final_status": final.project.status.value,
            "plan_versions": [plan.version for plan in final.plans],
            "task_statuses": {task.id: task.status.value for task in final.tasks},
            "plan_calls": supervisor.plan_calls,
            "impact_calls": supervisor.impact_calls,
            "review_order": supervisor.review_task_ids,
            "session_count": len(sessions),
            "workspace_test_returncode": verification.returncode,
            "failure_types": [
                event.metadata["error_type"]
                for event in final.events
                if "error_type" in event.metadata
            ],
            "cli_errors": [
                line
                for line in errors.getvalue().splitlines()
                if line.startswith("ERROR:")
            ],
            "default_output_readable": (
                "Status      Completed" in default_output
                and "project_status:" not in default_output
                and "execution_stop_reason:" not in default_output
            ),
            "verbose_output_auditable": (
                "project_status: done" in verbose_output
                and "active_plan_id:" in verbose_output
                and "current_task_id:" in verbose_output
            ),
            "human_inspect_readable": (
                "ACTION REQUIRED" in inspect_output
                and "No action has been executed." in inspect_output
                and "Review the fake push request" in inspect_output
            ),
            "renderers_closed": all(renderer.closed for renderer in renderers),
        }
        print(json.dumps(payload, sort_keys=True))
        passed = (
            payload["exit_codes"] == [0, 0, 0, 0, 0, 0]
            and safe_point.project.status is ProjectStatus.CHANGE_REQUESTED
            and safe_point.project.current_task_id is None
            and final.project.status is ProjectStatus.DONE
            and [plan.version for plan in final.plans] == [1, 2]
            and all(task.status is TaskStatus.COMPLETED for task in final.tasks)
            and supervisor.plan_calls == 1
            and supervisor.impact_calls == 1
            and verification.returncode == 0
            and len(sessions) == 4
            and payload["default_output_readable"]
            and payload["verbose_output_auditable"]
            and payload["human_inspect_readable"]
            and payload["renderers_closed"]
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
