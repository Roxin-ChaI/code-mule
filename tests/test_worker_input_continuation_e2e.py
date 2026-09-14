"""Deterministic local E2E for Worker input, answer, and Git delivery."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from io import StringIO
import json
import subprocess
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain.enums import (
    HumanActionStatus,
    HumanActionCategory,
    PlanStatus,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import ExecutionReport
from code_mule.git_delivery import GitDeliveryService
from code_mule.runtime import (
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler
from code_mule.state.store import JsonProjectStateStore
from code_mule.supervisor.contracts import ReviewResult
from code_mule.worker import CodexUserInputRequired, WorkerInputRequest
from code_mule.worker.structured_report import parse_structured_worker_report
from code_mule.worker.parsing import build_execution_report
from code_mule.cli.contracts import InvalidCliProjectState

from state import make_project_state


NOW = datetime(2026, 9, 5, tzinfo=UTC)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=root, text=True, check=True, capture_output=True
    ).stdout.strip()


class InputSession:
    thread_id = "thread-input"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.started = 0
        self.closed = 0

    def start(self):
        self.started += 1

    def execute(self, request, *, report_id, created_at):
        (self.workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        raise CodexUserInputRequired(
            WorkerInputRequest(
                "item/tool/requestUserInput",
                "worker-request-1",
                "Should multiply accept integers only?",
                ("Yes", "No"),
            )
        )

    def close(self):
        self.closed += 1


class ContinuationSession:
    thread_id = "thread-continuation"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.started = 0
        self.closed = 0
        self.prompts = []

    def start(self):
        self.started += 1

    def execute(self, request, *, report_id, created_at):
        self.prompts.append(request.prompt)
        self.assert_partial_work_is_present()
        (self.workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def multiply(a, b):\n    return a * b\n",
            encoding="utf-8",
        )
        (self.workspace / "test_calculator.py").write_text(
            "from calculator import multiply\n\n"
            "def test_multiply():\n    assert multiply(2, 3) == 6\n",
            encoding="utf-8",
        )
        return ExecutionReport(
            report_id,
            request.task.id,
            request.task.execution_attempts + 1,
            "completed",
            ("calculator.py", "test_calculator.py"),
            ("local test: pass",),
            ("compileall: pass",),
            "dirty",
            (),
            None,
            "continued the original task",
            created_at,
        )

    def assert_partial_work_is_present(self):
        if "def add" not in (self.workspace / "calculator.py").read_text():
            raise AssertionError("fresh Worker did not receive preserved partial work")

    def close(self):
        self.closed += 1


class ReportActionSession(InputSession):
    def __init__(self, workspace, kind, mutation=None):
        super().__init__(workspace)
        self.kind = kind
        self.mutation = mutation

    def execute(self, request, *, report_id, created_at):
        (self.workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        if self.mutation == "staged":
            git(self.workspace, "add", "--", "calculator.py")
        elif self.mutation == "head":
            git(self.workspace, "commit", "--allow-empty", "-q", "-m", "external")
        details = {
            "input": ("Boss storage choice required", "排行榜使用 localStorage 还是 session memory?", ["localStorage", "session memory"]),
            "approval": ("Installing a system package changes the host", "Approve installing the system package", []),
            "external_side_effect": ("Publishing the branch exposes project code remotely", "Push this branch to the remote repository", []),
        }[self.kind]
        payload = {
            "status": "completed",
            "summary": "Turn ended awaiting Boss",
            "files_changed": [] if self.mutation == "omitted" else ["calculator.py"],
            "tests": [],
            "static_checks": [],
            "git_state": "dirty",
            "issues": [],
            "human_action": {
                "kind": self.kind,
                "summary": details[0],
                "request": details[1],
                "choices": details[2],
            },
        }
        return build_execution_report(
            request, parse_structured_worker_report(json.dumps(payload)), report_id, created_at
        )


class AcceptingSupervisor:
    def __init__(self):
        self.reviews = []

    def review(self, request):
        self.reviews.append(request)
        return ReviewResult(
            SupervisorDecisionType.CONTINUE,
            "Task criteria satisfied",
            None,
            (),
        )


class DoneFinalizer:
    def __init__(self, store):
        self.store = store

    def finalize(self, state):
        updated = replace(
            state,
            project=replace(state.project, status=ProjectStatus.DONE),
            plans=tuple(replace(plan, status=PlanStatus.COMPLETED) for plan in state.plans),
        )
        self.store.save(updated)
        return updated


class WorkerInputContinuationE2ETests(unittest.TestCase):
    def test_input_answer_fresh_continuation_produces_one_commit(self):
        self._input_scenario()

    def test_report_input_answer_fresh_continuation_produces_one_commit(self):
        self._input_scenario("input")

    def test_report_approval_and_external_effect_only_accept_scoped_approval_or_rejection(self):
        for kind in ("approval", "external_side_effect"):
            for resolution in ("approve", "reject"):
                with self.subTest(kind=kind, resolution=resolution):
                    self._input_scenario(kind, resolution=resolution)

    def test_report_human_action_git_drift_fails_closed_before_input_or_review(self):
        for mutation in ("staged", "head", "omitted"):
            with self.subTest(mutation=mutation):
                self._input_scenario("input", mutation=mutation)

    def _input_scenario(self, report_kind=None, *, resolution=None, mutation=None):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            git(workspace, "init", "-q")
            git(workspace, "config", "user.name", "Code Mule Test")
            git(workspace, "config", "user.email", "code-mule@example.invalid")
            (workspace / "README.md").write_text("# fixture\n", encoding="utf-8")
            git(workspace, "add", "--", "README.md")
            git(workspace, "commit", "-q", "-m", "initial")
            initial_head = git(workspace, "rev-parse", "HEAD")

            state_file = root / "state" / "project.json"
            state_file.parent.mkdir()
            store = JsonProjectStateStore(state_file)
            source = make_project_state()
            task = replace(
                source.tasks[0],
                title="Add multiply support",
                description="Implement add and multiply with tests",
                status=TaskStatus.PENDING,
                dependencies=(),
                execution_attempts=0,
            )
            store.save(
                replace(
                    source,
                    project=replace(
                        source.project,
                        status=ProjectStatus.RUNNING,
                        current_task_id=None,
                        workspace=str(workspace),
                    ),
                    tasks=(task,),
                    decisions=(),
                    execution_reports=(),
                    change_requests=(),
                    impact_analyses=(),
                    events=(),
                )
            )

            input_session = (
                InputSession(workspace) if report_kind is None
                else ReportActionSession(workspace, report_kind, mutation)
            )
            continuation_session = ContinuationSession(workspace)
            sessions = [input_session, continuation_session]
            supervisor = AcceptingSupervisor()
            report_ids = iter(("report-input", "report-final"))

            def cycle_factory():
                session = sessions.pop(0)
                return TaskCycleService(
                    worker_session_factory=lambda: session,
                    supervisor=supervisor,
                    store=store,
                    clock=lambda: NOW,
                    report_id_factory=lambda: next(report_ids),
                    decision_id_factory=lambda: "decision-final",
                    event_id_factory=lambda: "event-runtime",
                    config=TaskCycleConfig(2),
                    git_delivery=GitDeliveryService(workspace, clock=lambda: NOW),
                )

            execution = ProjectExecutionService(
                store=store,
                scheduler=TaskScheduler(),
                task_cycle_factory=cycle_factory,
                prompt_builder=TaskPromptBuilder(),
                clock=lambda: NOW,
                event_id_factory=lambda: "event-execution",
                config=ProjectExecutionConfig(10),
                finalizer=DoneFinalizer(store),
            )
            composition = ProductionCliComposition(
                state_file,
                environment={},
                stdout=StringIO(),
                stderr=StringIO(),
                runtime_factory=lambda state: RuntimeComposition(
                    supervisor=object(),
                    worker_service=object(),
                    planning=object(),
                    execution=execution,
                    change_execution=object(),
                    renderer=type(
                        "Renderer",
                        (),
                        {"__enter__": lambda self: self, "__exit__": lambda *args: None},
                    )(),
                ),
            )

            first = composition.run(None)
            self.assertNotEqual(first.exit_code, 0)
            gated = store.load()
            action = gated.human_actions[-1]
            self.assertEqual(supervisor.reviews, [])
            self.assertIs(gated.project.status, ProjectStatus.HUMAN_REQUIRED)
            self.assertIs(gated.tasks[0].status, TaskStatus.IN_PROGRESS)
            self.assertEqual(gated.git_commit_results, ())
            self.assertEqual(input_session.closed, 1)
            if mutation:
                self.assertIs(action.category, HumanActionCategory.RECOVERY_UNCERTAIN)
                self.assertEqual(continuation_session.started, 0)
                self.assertTrue((workspace / "calculator.py").exists())
                return
            if report_kind in ("approval", "external_side_effect"):
                self.assertIs(action.category, {
                    "approval": HumanActionCategory.WORKER_APPROVAL,
                    "external_side_effect": HumanActionCategory.EXTERNAL_SIDE_EFFECT,
                }[report_kind])
                inspected = "\n".join(composition.inspect().output)
                self.assertIn(action.requested_action, inspected)
                self.assertIn(action.risk, inspected)
                self.assertIn("Approval type Product / report approval", inspected)
                self.assertIn(
                    "Protocol event Worker structured report (not a native request)",
                    inspected,
                )
                with self.assertRaises(InvalidCliProjectState):
                    composition.answer(action.id, "yes")
                self.assertEqual(store.load(), gated)
                getattr(composition, resolution)(action.id)
                closed = store.load()
                self.assertIs(closed.human_actions[-1].status, {
                    "approve": HumanActionStatus.APPROVED,
                    "reject": HumanActionStatus.REJECTED,
                }[resolution])
                self.assertIs(closed.project.status, ProjectStatus.HUMAN_REQUIRED)
                self.assertEqual(continuation_session.started, 0)
                self.assertEqual(git(workspace, "rev-parse", "HEAD"), initial_head)
                self.assertTrue((workspace / "calculator.py").exists())
                return
            self.assertIs(action.category, HumanActionCategory.WORKER_INPUT)
            self.assertEqual(action.task_id, task.id)
            if report_kind:
                self.assertEqual(gated.execution_reports[0].human_action.kind.value, report_kind)
                self.assertEqual(action.worker_input.worker_attempt, 1)
                self.assertEqual(action.worker_input.request_id, "report-input")
            self.assertEqual(action.worker_input.partial_paths, ("calculator.py",))
            self.assertEqual(action.worker_input.baseline_head, initial_head)
            self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "1")

            inspected = "\n".join(composition.inspect().output)
            self.assertIn(action.worker_input.question, inspected)
            for choice in action.worker_input.choices:
                self.assertIn(choice, inspected)
            answer = "localStorage" if report_kind else "Yes, integers only"
            answered = composition.answer(action.id, answer)
            self.assertIn("WORKER INPUT ANSWERED", answered.output)
            after_answer = store.load()
            self.assertIs(after_answer.project.status, ProjectStatus.RUNNING)
            self.assertIs(after_answer.tasks[0].status, TaskStatus.REOPENED)
            self.assertIs(
                after_answer.human_actions[-1].status, HumanActionStatus.RESOLVED
            )
            self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "1")

            final = composition.run(None)
            self.assertEqual(final.exit_code, 0)
            completed = store.load()
            self.assertIs(completed.project.status, ProjectStatus.DONE)
            self.assertIs(completed.tasks[0].status, TaskStatus.COMPLETED)
            self.assertEqual(len(completed.git_baselines), 1)
            self.assertEqual(len(completed.git_commit_results), 1)
            self.assertEqual(completed.git_baselines[0].baseline_head, initial_head)
            self.assertEqual(len(completed.tasks), 1)
            self.assertEqual(completed.tasks[0].id, task.id)
            self.assertEqual(completed.change_requests, ())
            self.assertEqual(completed.impact_analyses, ())
            self.assertEqual(len(supervisor.reviews), 1)
            self.assertEqual(completed.tasks[0].execution_attempts, 2 if report_kind else 1)
            self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "2")
            self.assertEqual(git(workspace, "status", "--short"), "")
            self.assertEqual(sessions, [])
            self.assertEqual(input_session.closed, 1)
            self.assertEqual(continuation_session.started, 1)
            self.assertEqual(continuation_session.closed, 1)
            self.assertIn(
                f"Boss answer: {answer}", continuation_session.prompts[0]
            )


if __name__ == "__main__":
    unittest.main()
