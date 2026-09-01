"""Real local Codex + fake Supervisor CHANGE replanning smoke."""

from datetime import UTC, datetime
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

from code_mule.domain.enums import (  # noqa: E402
    PlanStatus,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.orchestrator import ChangeCommand, OrchestratorService  # noqa: E402
from code_mule.planning import (  # noqa: E402
    ProjectPlanningRequest,
    ProjectPlanningService,
)
from code_mule.progress import (  # noqa: E402
    CompositeProgressSink,
    ConsoleProgressRenderer,
    ProgressEventType,
    RecordingProgressSink,
)
from code_mule.replanning import (  # noqa: E402
    ChangeExecutionService,
    ChangeReplanningRequest,
    ChangeReplanningService,
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
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor import (  # noqa: E402
    ImpactAnalysisResult,
    MilestoneProposal,
    PlanProposal,
    RequirementProposal,
    ReviewResult,
    TaskProposal,
)
from code_mule.worker import CodexWorkerConfig, CodexWorkerSession  # noqa: E402
from scripts.local_codex_autonomous_smoke import (  # noqa: E402
    _IdFactory,
    _empty_state,
)


OBJECTIVE = "Create a calculator with add and subtract plus tests."
CHANGE = "Add multiply support and tests."


class _FakeChangeSupervisor:
    def __init__(self) -> None:
        self.plan_calls = 0
        self.impact_calls = 0
        self.review_task_ids: list[str] = []

    def plan(self, request: object) -> PlanProposal:
        self.plan_calls += 1
        return PlanProposal(
            summary="Create add/subtract implementation and tests.",
            requirements=(
                RequirementProposal(
                    "REQ-BASE",
                    "Add and subtract",
                    "Provide tested add and subtract functions.",
                    "high",
                    ("add and subtract unit tests pass",),
                ),
            ),
            requirements_considered=(),
            milestones=(
                MilestoneProposal(
                    "M-BASE",
                    "Base calculator",
                    ("create-calculator", "test-add-subtract"),
                ),
            ),
            tasks=(
                TaskProposal(
                    "create-calculator",
                    "Create calculator",
                    "Create calculator.py with add(a, b) and subtract(a, b). Do not create tests yet.",
                    (),
                    ("calculator.py implements add and subtract",),
                    ("REQ-BASE",),
                ),
                TaskProposal(
                    "test-add-subtract",
                    "Test add and subtract",
                    "Create unittest coverage for add and subtract and run it.",
                    ("create-calculator",),
                    ("add and subtract tests pass",),
                    ("REQ-BASE",),
                ),
            ),
            risks=(),
            rationale="Two bounded dependent tasks.",
        )

    def analyze_change(self, request: object) -> ImpactAnalysisResult:
        self.impact_calls += 1
        change = request.change_request  # type: ignore[attr-defined]
        return ImpactAnalysisResult(
            change_request_id=change.id,
            summary="Add multiplication while preserving completed base work.",
            architecture_impact="Extend the calculator module and its tests.",
            affected_components=("calculator.py", "test_calculator.py"),
            affected_requirement_ids=(),
            affected_task_ids=(),
            affected_completed_tasks=(),
            affected_in_progress_tasks=(),
            affected_pending_tasks=(),
            requirements_to_add=(
                RequirementProposal(
                    "REQ-MULTIPLY",
                    "Multiply",
                    "Provide tested multiplication.",
                    "high",
                    ("multiply unit tests pass",),
                ),
            ),
            requirements_to_update=(),
            tasks_to_add=(
                TaskProposal(
                    "add-multiply",
                    "Add multiplication",
                    "Extend calculator.py with multiply(a, b) returning a * b.",
                    ("create-calculator",),
                    ("calculator.py implements multiply",),
                    ("REQ-MULTIPLY",),
                ),
                TaskProposal(
                    "test-multiply",
                    "Test multiplication",
                    "Extend unittest coverage for multiply and run all calculator tests.",
                    ("add-multiply",),
                    ("multiply tests pass with the existing calculator tests",),
                    ("REQ-MULTIPLY",),
                ),
            ),
            tasks_to_reopen=(),
            tasks_to_cancel=(),
            milestones=(
                MilestoneProposal(
                    "M-CHANGED",
                    "Changed calculator",
                    (
                        "create-calculator",
                        "test-add-subtract",
                        "add-multiply",
                        "test-multiply",
                    ),
                ),
            ),
            dependency_changes=(),
            risks=(),
            recommendation="Create Plan v2 and resume.",
            rationale="The change is additive and keeps completed work.",
        )

    def review(self, request: object) -> ReviewResult:
        task = request.task  # type: ignore[attr-defined]
        self.review_task_ids.append(task.id)
        return ReviewResult(
            SupervisorDecisionType.CONTINUE,
            "Fixed local smoke policy accepts structured local evidence.",
            None,
            (),
        )


class _ChangeAfterTaskCycle:
    def __init__(self, cycle: TaskCycleService, inject_change) -> None:
        self._cycle = cycle
        self._inject_change = inject_change

    def execute(self, request):
        outcome = self._cycle.execute(request)
        if not outcome.human_action_required:
            self._inject_change(request.task.id)
        return outcome


def main() -> int:
    with TemporaryDirectory(prefix="code-mule-change-local-") as temporary:
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
        supervisor = _FakeChangeSupervisor()
        sessions: list[CodexWorkerSession] = []
        clock = lambda: datetime.now(UTC)
        renderer = ConsoleProgressRenderer(sys.stderr)
        recording = RecordingProgressSink()
        progress = CompositeProgressSink((renderer, recording))
        report_ids = _IdFactory("change-report")
        decision_ids = _IdFactory("change-decision")
        cycle_event_ids = _IdFactory("change-cycle-event")

        planning = ProjectPlanningService(
            store=store,
            supervisor=supervisor,
            clock=clock,
            plan_id_factory=lambda: "PLAN-1",
            event_id_factory=_IdFactory("planning-event"),
            progress_sink=progress,
        )
        orchestrator = OrchestratorService(
            store,
            clock=clock,
            event_id_factory=_IdFactory("boss-event"),
            progress_sink=progress,
        )
        change_injected = False

        def inject_change(task_id: str) -> None:
            nonlocal change_injected
            if task_id != "create-calculator" or change_injected:
                return
            orchestrator.change(
                ChangeCommand(
                    "autonomous-local",
                    CHANGE,
                    "boss",
                    "CHANGE-MULTIPLY",
                )
            )
            change_injected = True

        def task_cycle_factory():
            def worker_session_factory() -> CodexWorkerSession:
                session = CodexWorkerSession(
                    CodexWorkerConfig(
                        command=("codex", "app-server"),
                        workspace=workspace,
                        approval_policy="on-request",
                        sandbox="workspace-write",
                        read_timeout_seconds=360,
                    ),
                    progress_sink=progress,
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
                progress_sink=progress,
            )
            return _ChangeAfterTaskCycle(cycle, inject_change)

        execution = ProjectExecutionService(
            store=store,
            scheduler=TaskScheduler(),
            task_cycle_factory=task_cycle_factory,
            prompt_builder=TaskPromptBuilder(),
            clock=clock,
            event_id_factory=_IdFactory("execution-event"),
            config=ProjectExecutionConfig(max_tasks_per_run=10),
            progress_sink=progress,
        )
        replanning = ChangeReplanningService(
            store=store,
            supervisor=supervisor,
            clock=clock,
            plan_id_factory=lambda: "PLAN-2",
            event_id_factory=_IdFactory("replanning-event"),
            progress_sink=progress,
        )
        changed_execution = ChangeExecutionService(
            replanning_service=replanning,
            execution_service=execution,
        )

        with renderer:
            planning_outcome = planning.plan(
                ProjectPlanningRequest("autonomous-local", OBJECTIVE)
            )
            before_change = execution.run()
            safe_point_state = store.load()
            completed_before_replan = tuple(
                task.id
                for task in safe_point_state.tasks
                if task.status is TaskStatus.COMPLETED
            )
            change_outcome = changed_execution.apply_and_resume(
                ChangeReplanningRequest(
                    "autonomous-local", "CHANGE-MULTIPLY"
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
        event_types = tuple(event.type for event in recording.events)
        persisted_events = tuple(item.event_type for item in final_state.events)
        task_statuses = {item.id: item.status.value for item in final_state.tasks}
        payload = {
            "workspace_type": "disposable temporary git repository",
            "objective": OBJECTIVE,
            "change": CHANGE,
            "initial_plan": planning_outcome.plan_id,
            "safe_point_stop": before_change.stop_reason.value,
            "tasks_before_replan": list(before_change.task_ids),
            "completed_before_replan": list(completed_before_replan),
            "replacement_plan": change_outcome.replanning.plan_id,
            "replacement_version": change_outcome.replanning.plan_version,
            "resumed_tasks": list(change_outcome.execution.task_ids),
            "task_statuses": task_statuses,
            "plan_statuses": [item.status.value for item in final_state.plans],
            "project_status": final_state.project.status.value,
            "plan_calls": supervisor.plan_calls,
            "impact_calls": supervisor.impact_calls,
            "review_order": supervisor.review_task_ids,
            "session_count": len(sessions),
            "independent_test_returncode": verification.returncode,
            "replanning_events": [
                item for item in persisted_events if "replanning" in item
            ],
        }
        print(json.dumps(payload, sort_keys=True))
        resumed = change_outcome.execution
        passed = (
            before_change.stop_reason
            is ProjectExecutionStopReason.CHANGE_REQUESTED
            and before_change.task_ids == ("create-calculator",)
            and completed_before_replan == ("create-calculator",)
            and safe_point_state.project.current_task_id is None
            and safe_point_state.tasks[1].status is TaskStatus.PENDING
            and change_outcome.replanning.previous_plan_version == 1
            and change_outcome.replanning.plan_version == 2
            and resumed is not None
            and resumed.task_ids
            == ("test-add-subtract", "add-multiply", "test-multiply")
            and final_state.project.status is ProjectStatus.DONE
            and tuple(item.status for item in final_state.plans)
            == (PlanStatus.SUPERSEDED, PlanStatus.COMPLETED)
            and all(
                task.status is TaskStatus.COMPLETED
                for task in final_state.tasks
            )
            and supervisor.plan_calls == 1
            and supervisor.impact_calls == 1
            and verification.returncode == 0
            and ProgressEventType.CHANGE_REQUESTED in event_types
            and ProgressEventType.REPLANNING_COMPLETED in event_types
            and "replanning.completed" in persisted_events
            and len(sessions) == 4
            and renderer.closed
            and not renderer.thread_alive
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
