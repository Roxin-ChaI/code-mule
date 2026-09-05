"""Disposable full-system release-readiness E2E without model API calls."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from code_mule.domain import (  # noqa: E402
    ExecutionReport,
    ProjectStatus,
    TaskStatus,
)
from code_mule.execution.service import ExecutionOwnershipService  # noqa: E402
from code_mule.git_delivery import GitDeliveryService  # noqa: E402
from code_mule.orchestrator import ChangeCommand, OrchestratorService  # noqa: E402
from code_mule.planning import ProjectPlanningRequest, ProjectPlanningService  # noqa: E402
from code_mule.progress import ConsoleProgressRenderer, ProgressSink  # noqa: E402
from code_mule.project_verification import (  # noqa: E402
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCommand,
    ProjectVerificationSpec,
)
from code_mule.project_verification.service import (  # noqa: E402
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.replanning import (  # noqa: E402
    ChangeExecutionService,
    ChangeReplanningRequest,
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
from code_mule.supervisor import FinalReviewResult  # noqa: E402
from code_mule.worker import (  # noqa: E402
    CodexWorkerConfig,
    CodexWorkerSession,
)
from scripts.local_codex_autonomous_smoke import _IdFactory, _empty_state  # noqa: E402
from scripts.local_codex_change_replanning_smoke import (  # noqa: E402
    CHANGE,
    OBJECTIVE,
    _ChangeAfterTaskCycle,
    _FakeChangeSupervisor,
)
from scripts.local_project_cancellation_e2e import active_scenario  # noqa: E402


DEFAULT_RELEASE_WORKER_TIMEOUT_SECONDS = 240.0


@dataclass(frozen=True)
class ReleaseInterruption:
    stage: str
    task_id: str | None
    project_status: str
    recovery_required: bool
    sessions_closed: bool
    lease_statuses: tuple[str, ...]


class ReleaseScenarioInterrupted(RuntimeError):
    """Safe, typed projection of a Boss interrupt inside the disposable E2E."""

    def __init__(self, details: ReleaseInterruption) -> None:
        super().__init__("manual release E2E interrupted")
        self.details = details


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ReleaseSupervisor(_FakeChangeSupervisor):
    def __init__(self) -> None:
        super().__init__()
        self.final_review_calls = 0

    def final_review(self, request):
        self.final_review_calls += 1
        return FinalReviewResult(
            FinalReviewDecision.APPROVE,
            "Fake final Supervisor approved deterministic release evidence.",
            (),
        )


class FakeReleaseWorkerSession:
    _next_id = 0

    def __init__(self, repository: Path) -> None:
        type(self)._next_id += 1
        self.repository = repository
        self._thread_id = f"fake-release-thread-{type(self)._next_id}"

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def start(self) -> None:
        return None

    def execute(self, request, *, report_id, created_at):
        task_id = request.task.id
        if task_id == "create-calculator":
            (self.repository / "calculator.py").write_text(
                "def add(left, right):\n    return left + right\n\n"
                "def subtract(left, right):\n    return left - right\n",
                encoding="utf-8",
            )
            paths = ("calculator.py",)
        elif task_id == "test-add-subtract":
            (self.repository / "test_calculator.py").write_text(
                "import unittest\n\nfrom calculator import add, subtract\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
                "    def test_subtract(self):\n        self.assertEqual(subtract(5, 3), 2)\n",
                encoding="utf-8",
            )
            paths = ("test_calculator.py",)
        elif task_id == "add-multiply":
            with (self.repository / "calculator.py").open("a", encoding="utf-8") as stream:
                stream.write("\n\ndef multiply(left, right):\n    return left * right\n")
            paths = ("calculator.py",)
        elif task_id == "test-multiply":
            target = self.repository / "test_calculator.py"
            contents = target.read_text(encoding="utf-8")
            contents = contents.replace(
                "from calculator import add, subtract",
                "from calculator import add, multiply, subtract",
            )
            contents += (
                "\n\nclass MultiplyTests(unittest.TestCase):\n"
                "    def test_multiply(self):\n"
                "        self.assertEqual(multiply(4, 3), 12)\n"
            )
            target.write_text(contents, encoding="utf-8")
            paths = ("test_calculator.py",)
        else:
            raise AssertionError(f"unexpected release Task: {task_id}")
        return ExecutionReport(
            report_id,
            task_id,
            request.task.execution_attempts + 1,
            "completed",
            paths,
            ("unittest: pass",),
            ("compileall: pass",),
            "dirty",
            (),
            None,
            "Deterministic fake Worker completed the release Task.",
            created_at,
        )

    def close(self) -> None:
        return None


def _repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir(parents=True)
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Code Mule Release E2E")
    git(repository, "config", "user.email", "code-mule@example.invalid")
    (repository / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (repository / "README.md").write_text("Release readiness E2E\n", encoding="utf-8")
    git(repository, "add", "--", ".gitignore", "README.md")
    git(repository, "commit", "-q", "-m", "initial")
    return repository


def run_release_scenario(
    *,
    real_worker: bool,
    supervisor=None,
    objective: str = OBJECTIVE,
    change: str = CHANGE,
    worker_timeout_seconds: float = DEFAULT_RELEASE_WORKER_TIMEOUT_SECONDS,
    progress_sink: ProgressSink | None = None,
    worker_session_factory: Callable[[Path], object] | None = None,
) -> dict[str, object]:
    if worker_timeout_seconds <= 0:
        raise ValueError("worker_timeout_seconds must be positive")
    with TemporaryDirectory(prefix="code-mule-release-e2e-") as directory:
        root = Path(directory)
        repository = _repository(root)
        state_file = root / ".code-mule" / "project-state.json"
        state_file.parent.mkdir()
        state = _empty_state(datetime.now(UTC))
        state = replace(
            state,
            project=replace(state.project, workspace=str(repository)),
            project_verification_spec=ProjectVerificationSpec(
                state.project.id,
                (
                    ProjectVerificationCommand(
                        "project unittest",
                        ProjectVerificationCategory.TEST,
                        (sys.executable, "-m", "unittest", "discover", "-s", ".", "-v"),
                        timeout_seconds=60,
                    ),
                ),
            ),
        )
        store = JsonProjectStateStore(state_file)
        store.save(state)
        supervisor = supervisor or ReleaseSupervisor()
        orchestrator = OrchestratorService(
            store,
            clock=lambda: datetime.now(UTC),
            event_id_factory=_IdFactory("boss-event"),
            progress_sink=progress_sink,
        )
        sessions = []
        change_injected = False
        report_ids = _IdFactory("report")
        decision_ids = _IdFactory("decision")
        task_event_ids = _IdFactory("task-event")
        execution_event_ids = _IdFactory("execution-event")
        worker_config = CodexWorkerConfig(
            command=("codex", "app-server"),
            workspace=repository,
            approval_policy="on-request",
            sandbox="workspace-write",
            inactivity_timeout_seconds=worker_timeout_seconds,
            max_turn_seconds=worker_timeout_seconds,
        )

        ownership = ExecutionOwnershipService(
            store=store,
            lock_path=state_file.parent / "execution.lock",
            clock=lambda: datetime.now(UTC),
            lease_id_factory=_IdFactory("lease"),
            owner_id_factory=_IdFactory("owner"),
            event_id_factory=_IdFactory("ownership-event"),
        )

        def inject_change(task_id: str) -> None:
            nonlocal change_injected
            if change_injected:
                return
            orchestrator.change(
                ChangeCommand(state.project.id, change, "boss", "CHANGE-MULTIPLY")
            )
            change_injected = True

        def execution_for(owner, *, inject: bool) -> ProjectExecutionService:
            def cycle_factory():
                def session_factory():
                    session = (
                        worker_session_factory(repository)
                        if worker_session_factory is not None
                        else CodexWorkerSession(
                            worker_config,
                            progress_sink=progress_sink,
                            clock=lambda: datetime.now(UTC),
                        )
                        if real_worker
                        else FakeReleaseWorkerSession(repository)
                    )
                    sessions.append(session)
                    return session

                cycle = TaskCycleService(
                    worker_session_factory=session_factory,
                    supervisor=supervisor,
                    store=store,
                    clock=lambda: datetime.now(UTC),
                    report_id_factory=report_ids,
                    decision_id_factory=decision_ids,
                    event_id_factory=task_event_ids,
                    config=TaskCycleConfig(max_attempts=2),
                    progress_sink=progress_sink,
                    worker_identity_started=owner.record_worker_identity,
                    worker_identity_cleared=owner.clear_worker_identity,
                    git_delivery=GitDeliveryService(
                        repository, clock=lambda: datetime.now(UTC)
                    ),
                )
                return _ChangeAfterTaskCycle(cycle, inject_change) if inject else cycle

            return ProjectExecutionService(
                store=store,
                scheduler=TaskScheduler(),
                task_cycle_factory=cycle_factory,
                prompt_builder=TaskPromptBuilder(),
                clock=lambda: datetime.now(UTC),
                event_id_factory=execution_event_ids,
                config=ProjectExecutionConfig(max_tasks_per_run=20),
                progress_sink=progress_sink,
                finalizer=ProjectFinalizationService(
                    store=store,
                    verification=ProjectVerificationService(
                        clock=lambda: datetime.now(UTC),
                        result_id_factory=_IdFactory("verification"),
                        environment={"PATH": os.environ.get("PATH", "")},
                    ),
                    supervisor=supervisor,
                    clock=lambda: datetime.now(UTC),
                    event_id_factory=_IdFactory("final-event"),
                    progress_sink=progress_sink,
                ),
            )

        try:
            with ownership.acquire() as owner:
                planning = ProjectPlanningService(
                    store=store,
                    supervisor=supervisor,
                    clock=lambda: datetime.now(UTC),
                    plan_id_factory=lambda: "PLAN-1",
                    event_id_factory=_IdFactory("planning-event"),
                    progress_sink=progress_sink,
                ).plan(ProjectPlanningRequest(state.project.id, objective))
                first_execution = execution_for(owner, inject=True).run()
        except KeyboardInterrupt:
            raise ReleaseScenarioInterrupted(
                _interruption_details(store, sessions, "initial execution")
            ) from None

        safe_point = store.load()
        if safe_point.project.status is not ProjectStatus.CHANGE_REQUESTED:
            session_ids = [session.thread_id for session in sessions]
            return {
                "worker": "real_codex" if real_worker else "fake",
                "planning_ready": planning.ready_for_execution,
                "safe_point_status": safe_point.project.status.value,
                "safe_point_current_task": safe_point.project.current_task_id,
                "first_stop_reason": first_execution.stop_reason.value,
                "replacement_version": None,
                "final_status": safe_point.project.status.value,
                "task_statuses": {
                    item.id: item.status.value for item in safe_point.tasks
                },
                "session_count": len(sessions),
                "unique_session_count": len(set(session_ids)),
                "delivery_commit_count": len(safe_point.git_commit_results),
                "repository_commit_count": int(
                    git(repository, "rev-list", "--count", "HEAD")
                ) - 1,
                "workspace_clean": git(repository, "status", "--short") == "",
                "verification_passed": False,
                "final_review": None,
                "final_review_calls": getattr(
                    supervisor, "final_review_calls", None
                ),
                "lease_statuses": [
                    item.status.value for item in safe_point.execution_leases
                ],
                "pending_action_categories": [
                    item.category.value
                    for item in safe_point.human_actions
                    if item.status.value == "pending"
                ],
                "failure_types": [
                    event.metadata["error_type"]
                    for event in safe_point.events
                    if "error_type" in event.metadata
                ],
            }
        try:
            with ownership.acquire() as owner:
                execution = execution_for(owner, inject=False)
                replanning = ChangeReplanningService(
                    store=store,
                    supervisor=supervisor,
                    clock=lambda: datetime.now(UTC),
                    plan_id_factory=lambda: "PLAN-2",
                    event_id_factory=_IdFactory("replanning-event"),
                    progress_sink=progress_sink,
                )
                changed = ChangeExecutionService(
                    replanning_service=replanning,
                    execution_service=execution,
                ).apply_and_resume(
                    ChangeReplanningRequest(state.project.id, "CHANGE-MULTIPLY")
                )
        except KeyboardInterrupt:
            raise ReleaseScenarioInterrupted(
                _interruption_details(store, sessions, "change replanning")
            ) from None

        final = store.load()
        verification = final.project_verification_results[-1]
        session_ids = [session.thread_id for session in sessions]
        commits = git(repository, "log", "--format=%H%x09%s").splitlines()
        return {
            "worker": "real_codex" if real_worker else "fake",
            "planning_ready": planning.ready_for_execution,
            "safe_point_status": safe_point.project.status.value,
            "safe_point_current_task": safe_point.project.current_task_id,
            "first_stop_reason": first_execution.stop_reason.value,
            "replacement_version": changed.replanning.plan_version,
            "final_status": final.project.status.value,
            "task_statuses": {item.id: item.status.value for item in final.tasks},
            "session_count": len(sessions),
            "unique_session_count": len(set(session_ids)),
            "delivery_commit_count": len(final.git_commit_results),
            "repository_commit_count": len(commits) - 1,
            "workspace_clean": git(repository, "status", "--short") == "",
            "verification_passed": verification.passed,
            "final_review": (
                None
                if verification.final_review_decision is None
                else verification.final_review_decision.value
            ),
            "final_review_calls": getattr(supervisor, "final_review_calls", None),
            "lease_statuses": [item.status.value for item in final.execution_leases],
        }


def _interruption_details(
    store: JsonProjectStateStore,
    sessions: list[object],
    fallback_stage: str,
) -> ReleaseInterruption:
    state = store.load()
    task_id = state.project.current_task_id
    stage = "worker" if task_id is not None else fallback_stage
    recovery_required = any(
        action.status.value == "pending"
        and action.category.value == "recovery_uncertain"
        for action in state.human_actions
    )
    return ReleaseInterruption(
        stage=stage,
        task_id=task_id,
        project_status=state.project.status.value,
        recovery_required=recovery_required,
        sessions_closed=all(bool(getattr(session, "closed", False)) for session in sessions),
        lease_statuses=tuple(item.status.value for item in state.execution_leases),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake-worker", action="store_true")
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=DEFAULT_RELEASE_WORKER_TIMEOUT_SECONDS,
    )
    arguments = parser.parse_args()
    try:
        with ConsoleProgressRenderer(sys.stderr) as progress:
            release = run_release_scenario(
                real_worker=not arguments.fake_worker,
                worker_timeout_seconds=arguments.worker_timeout_seconds,
                progress_sink=progress,
            )
    except ReleaseScenarioInterrupted as error:
        details = error.details
        print("LOCAL RELEASE E2E INTERRUPTED", file=sys.stderr)
        print(f"Stage: {details.stage}", file=sys.stderr)
        print(f"Task: {details.task_id or 'none'}", file=sys.stderr)
        print(f"Project state: {details.project_status.upper()}", file=sys.stderr)
        print(
            f"Recovery required: {'yes' if details.recovery_required else 'no'}",
            file=sys.stderr,
        )
        return 130
    with TemporaryDirectory(prefix="code-mule-release-cancel-") as directory:
        root = Path(directory)
        cancellation = active_scenario(root, real_worker=not arguments.fake_worker)
    payload = {"release": release, "cancellation": cancellation}
    print(json.dumps(payload, sort_keys=True))
    valid = (
        release["planning_ready"]
        and release["safe_point_status"] == "change_requested"
        and release["safe_point_current_task"] is None
        and release["first_stop_reason"] == "change_requested"
        and release["replacement_version"] == 2
        and release["final_status"] == ProjectStatus.DONE.value
        and all(status == TaskStatus.COMPLETED.value for status in release["task_statuses"].values())
        and release["session_count"] == 4
        and release["unique_session_count"] == 4
        and release["delivery_commit_count"] == 4
        and release["repository_commit_count"] == 4
        and release["workspace_clean"]
        and release["verification_passed"]
        and release["final_review"] == FinalReviewDecision.APPROVE.value
        and release["final_review_calls"] == 1
        and release["lease_statuses"] == ["released", "released"]
        and cancellation["project_status"] == "cancelled"
        and cancellation["task_ids_started"] == ["TASK-ADD"]
        and cancellation["workspace_clean"]
        and cancellation["finalizer_calls"] == 0
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
