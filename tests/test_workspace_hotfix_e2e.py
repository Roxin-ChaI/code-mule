from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from itertools import count
from pathlib import Path
import subprocess
import tempfile
import unittest

from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain import (
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    PlanStatus,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import ExecutionReport, Milestone, Plan, Task
from code_mule.git_delivery import GitDeliveryService
from code_mule.progress import ConsoleProgressRenderer
from code_mule.presentation import human_action_view
from code_mule.runtime import (
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler
from code_mule.supervisor.contracts import ReviewResult
from code_mule.state.store import JsonProjectStateStore


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


class _WorkerSession:
    def __init__(self, root: Path, starts: list[str]) -> None:
        self._root = root
        self._starts = starts

    @property
    def thread_id(self) -> str:
        return "fake-thread"

    def start(self) -> None:
        self._starts.append("started")

    def execute(self, request, *, report_id, created_at) -> ExecutionReport:
        (self._root / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        return ExecutionReport(
            report_id,
            request.task.id,
            request.task.execution_attempts + 1,
            "completed",
            ("feature.py",),
            ("unittest: pass",),
            ("compileall: pass",),
            "dirty",
            (),
            False,
            "implemented",
            created_at,
        )

    def close(self) -> None:
        return None


class _Supervisor:
    def review(self, request) -> ReviewResult:
        return ReviewResult(
            SupervisorDecisionType.CONTINUE,
            "deterministic acceptance",
            None,
            (),
        )


class _Finalizer:
    def __init__(self, store: JsonProjectStateStore) -> None:
        self._store = store

    def finalize(self, state):
        completed = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.DONE,
                current_task_id=None,
                updated_at=NOW,
            ),
            plans=tuple(
                replace(plan, status=PlanStatus.COMPLETED)
                if plan.id == state.project.active_plan_id
                else plan
                for plan in state.plans
            ),
        )
        self._store.save(completed)
        return completed


class WorkspaceHotfixE2E(unittest.TestCase):
    def test_workspace_block_is_repairable_without_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git(root, "init", "-q")
            git(root, "config", "user.name", "Code Mule Test")
            git(root, "config", "user.email", "code-mule@example.invalid")
            (root / "README.md").write_text("baseline\n", encoding="utf-8")
            git(root, "add", "--", "README.md")
            git(root, "commit", "-q", "-m", "initial")

            state_file = root / ".code-mule" / "project-state.json"
            starts: list[str] = []
            output = StringIO()
            errors = StringIO()
            composition = ProductionCliComposition(
                state_file,
                environment={},
                stdout=output,
                stderr=errors,
                runtime_factory=lambda state: self._runtime(
                    root, state_file, starts, errors
                ),
            )
            composition.init_project("project-1", "Project", root)
            self.assertEqual(git(root, "status", "--short"), "")
            self._materialize_plan(state_file)

            dirty = root / "user-notes.txt"
            dirty.write_text("unrelated\n", encoding="utf-8")
            first = composition.run(None)
            blocked = JsonProjectStateStore(state_file).load()
            action = blocked.human_actions[-1]
            self.assertEqual(first.exit_code, 4)
            self.assertEqual(starts, [])
            self.assertIs(action.category, HumanActionCategory.WORKSPACE_BLOCK)
            self.assertIs(action.status, HumanActionStatus.PENDING)
            self.assertEqual(action.task_id, "TASK-1")
            self.assertEqual(blocked.tasks[0].execution_attempts, 0)
            self.assertEqual(human_action_view(action).category, "Workspace block")

            dirty.unlink()
            composition.resolve(action.id, HumanResolutionStrategy.RETRY_TASK)
            reopened = JsonProjectStateStore(state_file).load()
            self.assertIs(reopened.project.status, ProjectStatus.RUNNING)
            self.assertIsNone(reopened.project.current_task_id)
            self.assertIs(reopened.tasks[0].status, TaskStatus.REOPENED)

            second = composition.run(None)
            completed = JsonProjectStateStore(state_file).load()
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(starts, ["started"])
            self.assertIs(completed.project.status, ProjectStatus.DONE)
            self.assertIs(completed.tasks[0].status, TaskStatus.COMPLETED)
            self.assertEqual(git(root, "status", "--short"), "")

    @staticmethod
    def _materialize_plan(state_file: Path) -> None:
        store = JsonProjectStateStore(state_file)
        state = store.load()
        plan = Plan("PLAN-1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), NOW)
        milestone = Milestone("M1", plan.id, "Work", "active", ("TASK-1",))
        task = Task(
            "TASK-1",
            milestone.id,
            "Add feature",
            "Create feature.py",
            TaskStatus.PENDING,
            (),
            ("feature exists",),
            0,
            NOW,
            NOW,
        )
        store.save(
            replace(
                state,
                project=replace(
                    state.project,
                    status=ProjectStatus.RUNNING,
                    active_plan_id=plan.id,
                    objective="Add a feature",
                    updated_at=NOW,
                ),
                plans=(plan,),
                milestones=(milestone,),
                tasks=(task,),
            )
        )

    @staticmethod
    def _runtime(
        root: Path,
        state_file: Path,
        starts: list[str],
        errors: StringIO,
    ) -> RuntimeComposition:
        store = JsonProjectStateStore(state_file)
        supervisor = _Supervisor()
        renderer = ConsoleProgressRenderer(errors)
        identifiers = count(len(store.load().events) + 1)

        def event_id() -> str:
            return f"HOTFIX-EVENT-{next(identifiers)}"

        def cycle_factory() -> TaskCycleService:
            return TaskCycleService(
                worker_session_factory=lambda: _WorkerSession(root, starts),
                supervisor=supervisor,
                store=store,
                clock=lambda: NOW,
                report_id_factory=lambda: "REPORT-1",
                decision_id_factory=lambda: "DECISION-1",
                event_id_factory=event_id,
                config=TaskCycleConfig(2),
                progress_sink=renderer,
                git_delivery=GitDeliveryService(root, clock=lambda: NOW),
            )

        execution = ProjectExecutionService(
            store=store,
            scheduler=TaskScheduler(),
            task_cycle_factory=cycle_factory,
            prompt_builder=TaskPromptBuilder(),
            clock=lambda: NOW,
            event_id_factory=event_id,
            config=ProjectExecutionConfig(10),
            progress_sink=renderer,
            finalizer=_Finalizer(store),
        )
        return RuntimeComposition(
            supervisor=supervisor,
            worker_service=object(),
            planning=object(),
            execution=execution,
            change_execution=object(),
            renderer=renderer,
        )


if __name__ == "__main__":
    unittest.main()
