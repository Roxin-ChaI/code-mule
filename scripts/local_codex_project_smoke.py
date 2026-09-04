"""Real local Codex + fake Supervisor two-task disposable project smoke."""

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
from code_mule.domain.models import Milestone, Plan, Project, Task  # noqa: E402
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
from code_mule.supervisor.contracts import ReviewResult  # noqa: E402
from code_mule.worker import CodexWorkerConfig, CodexWorkerSession  # noqa: E402


class _IdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._prefix}-{self._value}"


class _ContinueSupervisor:
    def __init__(self) -> None:
        self.task_ids: list[str] = []

    def review(self, request: object) -> ReviewResult:
        task = request.task  # type: ignore[attr-defined]
        self.task_ids.append(task.id)
        return ReviewResult(
            decision=SupervisorDecisionType.CONTINUE,
            rationale="Fixed local smoke policy accepts the structured evidence.",
            next_task_prompt=None,
            issues=(),
        )


def build_project_state(now: datetime) -> ProjectState:
    task_one = Task(
        id="create-math-utils",
        milestone_id="local-project-milestone",
        title="Create math utility",
        description=(
            "Create math_utils.py containing an add(a, b) function that returns "
            "a + b. Do not create the test file in this task."
        ),
        status=TaskStatus.PENDING,
        dependencies=(),
        acceptance_criteria=(
            "math_utils.py exists",
            "add(a, b) returns a + b",
            "test_math_utils.py is left for the dependent task",
        ),
        execution_attempts=0,
        created_at=now,
        updated_at=now,
    )
    task_two = Task(
        id="test-math-utils",
        milestone_id="local-project-milestone",
        title="Test math utility",
        description=(
            "Create test_math_utils.py using unittest and verify "
            "add(2, 3) == 5. Run the local unit test."
        ),
        status=TaskStatus.PENDING,
        dependencies=(task_one.id,),
        acceptance_criteria=(
            "test_math_utils.py exists",
            "the test asserts add(2, 3) == 5",
            "the local unittest passes",
        ),
        execution_attempts=0,
        created_at=now,
        updated_at=now,
    )
    milestone = Milestone(
        id="local-project-milestone",
        plan_id="local-project-plan",
        title="Implement and test addition",
        status="pending",
        task_ids=(task_one.id, task_two.id),
    )
    plan = Plan(
        id="local-project-plan",
        project_id="local-project",
        version=1,
        status=PlanStatus.ACTIVE,
        requirement_ids=(),
        milestone_ids=(milestone.id,),
        created_at=now,
    )
    project = Project(
        id="local-project",
        name="Disposable local Codex multi-task project",
        status=ProjectStatus.RUNNING,
        active_plan_id=plan.id,
        current_task_id=None,
        created_at=now,
        updated_at=now,
    )
    return ProjectState(
        project=project,
        requirements=(),
        plans=(plan,),
        milestones=(milestone,),
        tasks=(task_one, task_two),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


def main() -> int:
    with TemporaryDirectory(prefix="code-mule-local-project-") as temporary:
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
        store.save(build_project_state(datetime.now(UTC)))
        supervisor = _ContinueSupervisor()
        sessions: list[CodexWorkerSession] = []
        report_ids = _IdFactory("local-project-report")
        decision_ids = _IdFactory("local-project-decision")
        cycle_event_ids = _IdFactory("local-project-cycle-event")
        progress_clock = lambda: datetime.now(UTC)
        renderer = ConsoleProgressRenderer(sys.stderr)
        recording = RecordingProgressSink()
        progress = CompositeProgressSink((renderer, recording))

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
                    clock=progress_clock,
                )
                sessions.append(session)
                return session

            return TaskCycleService(
                worker_session_factory=worker_session_factory,
                supervisor=supervisor,
                store=store,
                clock=progress_clock,
                report_id_factory=report_ids,
                decision_id_factory=decision_ids,
                event_id_factory=cycle_event_ids,
                config=TaskCycleConfig(max_attempts=1),
                progress_sink=progress,
            )

        runtime = ProjectExecutionService(
            store=store,
            scheduler=TaskScheduler(),
            task_cycle_factory=task_cycle_factory,
            prompt_builder=TaskPromptBuilder(),
            clock=progress_clock,
            event_id_factory=_IdFactory("local-project-event"),
            config=ProjectExecutionConfig(max_tasks_per_run=2),
            progress_sink=progress,
        )
        with renderer:
            outcome = runtime.run()
        final_state = store.load()
        verification = subprocess.run(
            [sys.executable, "-m", "unittest", "-v"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        math_source = (workspace / "math_utils.py").read_text(encoding="utf-8")
        test_source = (workspace / "test_math_utils.py").read_text(encoding="utf-8")
        worker_event_types = [
            event.type.value
            for event in recording.events
            if event.type
            in {
                ProgressEventType.WORKER_STARTING,
                ProgressEventType.WORKER_STARTED,
                ProgressEventType.WORKER_ACTIVITY,
                ProgressEventType.WORKER_COMPLETED,
                ProgressEventType.WORKER_FAILED,
            }
        ]
        activity_types = [
            event.metadata["activity"]
            for event in recording.events
            if event.type is ProgressEventType.WORKER_ACTIVITY
            and "activity" in event.metadata
        ]
        payload = {
            "workspace_type": "disposable temporary git repository",
            "task_order": list(outcome.task_ids),
            "task_count": outcome.tasks_completed,
            "task_statuses": [task.status.value for task in final_state.tasks],
            "milestone_status": final_state.milestones[0].status,
            "plan_status": final_state.plans[0].status.value,
            "project_status": final_state.project.status.value,
            "session_count": len(sessions),
            "supervisor_review_order": supervisor.task_ids,
            "math_utils": math_source,
            "test_math_utils": test_source,
            "independent_test_returncode": verification.returncode,
            "progress_event_count": len(recording.events),
            "worker_events_observed": worker_event_types,
            "codex_activity_types": activity_types,
            "final_percentage": renderer.snapshot.percentage,
            "renderer_closed": renderer.closed,
            "renderer_thread_alive": renderer.thread_alive,
        }
        print(json.dumps(payload, sort_keys=True))
        passed = (
            outcome.stop_reason is ProjectExecutionStopReason.PLAN_COMPLETED
            and outcome.task_ids == ("create-math-utils", "test-math-utils")
            and outcome.tasks_completed == 2
            and supervisor.task_ids
            == ["create-math-utils", "test-math-utils"]
            and len(sessions) == 2
            and all(
                task.status is TaskStatus.COMPLETED for task in final_state.tasks
            )
            and final_state.milestones[0].status == "completed"
            and final_state.plans[0].status is PlanStatus.COMPLETED
            and final_state.project.status is ProjectStatus.DONE
            and "return a + b" in math_source
            and "add(2, 3)" in test_source
            and verification.returncode == 0
            and ProgressEventType.WORKER_STARTED.value in worker_event_types
            and ProgressEventType.WORKER_COMPLETED.value in worker_event_types
            and len(activity_types) >= 1
            and renderer.snapshot.percentage == 100.0
            and renderer.closed
            and not renderer.thread_alive
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
