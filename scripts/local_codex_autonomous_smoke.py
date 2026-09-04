"""Real local Codex + fake PLAN/REVIEW autonomous bootstrap smoke."""

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_mule.domain.enums import (  # noqa: E402
    PlanStatus,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import Project  # noqa: E402
from code_mule.planning import (  # noqa: E402
    AutonomousProjectService,
    ProjectPlanningRequest,
    ProjectPlanningService,
)
from code_mule.progress import (  # noqa: E402
    CompositeProgressSink,
    ConsoleProgressRenderer,
    ProgressEventType,
    RecordingProgressSink,
)
from code_mule.runtime import (  # noqa: E402
    ProjectExecutionConfig,
    ProjectExecutionService,
    ProjectExecutionStopReason,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler  # noqa: E402
from code_mule.state.models import ProjectState  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor import (  # noqa: E402
    MilestoneProposal,
    PlanProposal,
    RequirementProposal,
    ReviewResult,
    TaskProposal,
)
from code_mule.worker import CodexWorkerConfig, CodexWorkerSession  # noqa: E402


class _IdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._prefix}-{self._value}"


class _FakeSupervisor:
    def __init__(self) -> None:
        self.plan_calls = 0
        self.review_task_ids: list[str] = []

    def plan(self, request: object) -> PlanProposal:
        self.plan_calls += 1
        return PlanProposal(
            summary="Create and test a tiny calculator module",
            requirements=(
                RequirementProposal(
                    "REQ-CALCULATOR",
                    "Calculator addition",
                    "Provide a verified add function.",
                    "high",
                    ("add(2, 3) returns 5",),
                ),
            ),
            requirements_considered=(),
            milestones=(
                MilestoneProposal(
                    "M-CALCULATOR",
                    "Calculator implementation",
                    ("create-math-utils", "test-math-utils"),
                ),
            ),
            tasks=(
                TaskProposal(
                    "create-math-utils",
                    "Create math utility",
                    "Create math_utils.py with add(a, b) returning a + b. Do not create tests yet.",
                    (),
                    ("math_utils.py exists", "add(a, b) returns a + b"),
                    ("REQ-CALCULATOR",),
                ),
                TaskProposal(
                    "test-math-utils",
                    "Test math utility",
                    "Create unittest coverage asserting add(2, 3) == 5 and run it.",
                    ("create-math-utils",),
                    ("test_math_utils.py exists", "the local unittest passes"),
                    ("REQ-CALCULATOR",),
                ),
            ),
            risks=(),
            rationale="Two dependent, independently reviewable Codex cycles.",
        )

    def review(self, request: object) -> ReviewResult:
        task = request.task  # type: ignore[attr-defined]
        self.review_task_ids.append(task.id)
        return ReviewResult(
            SupervisorDecisionType.CONTINUE,
            "Fixed smoke policy accepts structured local evidence.",
            None,
            (),
        )


def _empty_state(now: datetime) -> ProjectState:
    return ProjectState(
        project=Project(
            "autonomous-local",
            "Autonomous local calculator",
            ProjectStatus.IDLE,
            None,
            None,
            now,
            now,
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


def main() -> int:
    with TemporaryDirectory(prefix="code-mule-autonomous-local-") as temporary:
        root = Path(temporary)
        workspace = root / "repository"
        workspace.mkdir()
        subprocess.run(
            ["git", "init"], cwd=workspace, check=True, capture_output=True, text=True
        )
        store = JsonProjectStateStore(root / "project-state.json")
        store.save(_empty_state(datetime.now(UTC)))
        supervisor = _FakeSupervisor()
        sessions: list[CodexWorkerSession] = []
        report_ids = _IdFactory("report")
        decision_ids = _IdFactory("decision")
        cycle_event_ids = _IdFactory("cycle-event")
        clock = lambda: datetime.now(UTC)
        renderer = ConsoleProgressRenderer(sys.stderr)
        recording = RecordingProgressSink()
        progress = CompositeProgressSink((renderer, recording))

        planning = ProjectPlanningService(
            store=store,
            supervisor=supervisor,
            clock=clock,
            plan_id_factory=lambda: "PLAN-CALCULATOR-1",
            event_id_factory=_IdFactory("planning-event"),
            progress_sink=progress,
        )

        def task_cycle_factory() -> TaskCycleService:
            def worker_session_factory() -> CodexWorkerSession:
                session = CodexWorkerSession(
                    CodexWorkerConfig(
                        command=("codex", "app-server"),
                        workspace=workspace,
                        approval_policy="on-request",
                        sandbox="workspace-write",
                        inactivity_timeout_seconds=120,
                        max_turn_seconds=900,
                    ),
                    progress_sink=progress,
                    clock=clock,
                )
                sessions.append(session)
                return session

            return TaskCycleService(
                worker_session_factory=worker_session_factory,
                supervisor=supervisor,
                store=store,
                clock=clock,
                report_id_factory=report_ids,
                decision_id_factory=decision_ids,
                event_id_factory=cycle_event_ids,
                config=TaskCycleConfig(max_attempts=1),
                progress_sink=progress,
            )

        execution = ProjectExecutionService(
            store=store,
            scheduler=TaskScheduler(),
            task_cycle_factory=task_cycle_factory,
            prompt_builder=TaskPromptBuilder(),
            clock=clock,
            event_id_factory=_IdFactory("project-event"),
            config=ProjectExecutionConfig(max_tasks_per_run=2),
            progress_sink=progress,
        )
        autonomous = AutonomousProjectService(
            planning_service=planning,
            execution_service=execution,
        )
        with renderer:
            outcome = autonomous.run_new_project(
                ProjectPlanningRequest(
                    "autonomous-local", "Create a tiny calculator library"
                )
            )

        final_state = store.load()
        verification = subprocess.run(
            [sys.executable, "-m", "unittest", "-v"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        execution_outcome = outcome.execution
        event_types = [event.type.value for event in recording.events]
        payload = {
            "workspace_type": "disposable temporary git repository",
            "boss_objective": "Create a tiny calculator library",
            "fake_plan_calls": supervisor.plan_calls,
            "plan_id": outcome.planning.plan_id,
            "plan_version": outcome.planning.plan_version,
            "requirement_ids": list(outcome.planning.requirement_ids),
            "task_order": [] if execution_outcome is None else list(execution_outcome.task_ids),
            "review_order": supervisor.review_task_ids,
            "session_count": len(sessions),
            "task_statuses": [task.status.value for task in final_state.tasks],
            "task_requirement_ids": [list(task.requirement_ids) for task in final_state.tasks],
            "plan_status": final_state.plans[0].status.value,
            "project_status": final_state.project.status.value,
            "planning_events": [item for item in event_types if "planning" in item or "plan_" in item],
            "worker_activity_count": event_types.count(ProgressEventType.WORKER_ACTIVITY.value),
            "final_percentage": renderer.snapshot.percentage,
            "renderer_closed": renderer.closed,
            "renderer_thread_alive": renderer.thread_alive,
            "independent_test_returncode": verification.returncode,
        }
        print(json.dumps(payload, sort_keys=True))
        passed = (
            supervisor.plan_calls == 1
            and execution_outcome is not None
            and execution_outcome.stop_reason is ProjectExecutionStopReason.PLAN_COMPLETED
            and execution_outcome.task_ids == ("create-math-utils", "test-math-utils")
            and supervisor.review_task_ids == ["create-math-utils", "test-math-utils"]
            and len(sessions) == 2
            and all(task.status is TaskStatus.COMPLETED for task in final_state.tasks)
            and all(task.requirement_ids == ("REQ-CALCULATOR",) for task in final_state.tasks)
            and final_state.plans[0].status is PlanStatus.COMPLETED
            and final_state.project.status is ProjectStatus.DONE
            and verification.returncode == 0
            and ProgressEventType.PLANNING_STARTED.value in event_types
            and ProgressEventType.PLANNING_COMPLETED.value in event_types
            and event_types.count(ProgressEventType.WORKER_ACTIVITY.value) >= 1
            and renderer.snapshot.percentage == 100.0
            and renderer.closed
            and not renderer.thread_alive
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
