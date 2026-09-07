from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.cli.contracts import CliExitCode
from code_mule.domain.enums import (
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import Milestone, Plan, Task
from code_mule.progress import ConsoleProgressRenderer
from code_mule.runtime import ProjectExecutionStopReason
from code_mule.state.store import JsonProjectStateStore

from tests.onboarding.helpers import chdir, git_repo


class _FakePlanning:
    def __init__(self, store):
        self.store = store

    def plan(self, request):
        state = self.store.load()
        now = datetime.now(UTC)
        plan = Plan("PLAN-1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), now)
        milestone = Milestone("M1", plan.id, "Work", "active", ("T1",))
        task = Task(
            "T1",
            "M1",
            "Work",
            "Do the work",
            TaskStatus.PENDING,
            (),
            ("done",),
            0,
            now,
            now,
        )
        self.store.save(
            replace(
                state,
                project=replace(
                    state.project,
                    status=ProjectStatus.RUNNING,
                    active_plan_id=plan.id,
                ),
                plans=(plan,),
                milestones=(milestone,),
                tasks=(task,),
            )
        )
        return SimpleNamespace(ready_for_execution=True, plan_id=plan.id)


class _FakeExecution:
    def __init__(self, store):
        self.store = store

    def run(self):
        state = self.store.load()
        self.store.save(
            replace(
                state,
                project=replace(
                    state.project,
                    status=ProjectStatus.DONE,
                    current_task_id=None,
                ),
                plans=tuple(
                    replace(plan, status=PlanStatus.COMPLETED)
                    for plan in state.plans
                ),
                tasks=tuple(
                    replace(task, status=TaskStatus.COMPLETED)
                    for task in state.tasks
                ),
            )
        )
        return SimpleNamespace(
            stop_reason=ProjectExecutionStopReason.PLAN_COMPLETED,
            human_action_required=False,
        )

    def recover(self, plan):
        return self.run()


def runtime_factory_for(state_file):
    def factory(state):
        store = JsonProjectStateStore(state_file)
        return RuntimeComposition(
            supervisor=object(),
            worker_service=object(),
            planning=_FakePlanning(store),
            execution=_FakeExecution(store),
            change_execution=object(),
            renderer=ConsoleProgressRenderer(StringIO()),
        )

    return factory


class StartPreflightAndCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = git_repo(self.root / "Calculator App")

    def tearDown(self):
        self.temporary.cleanup()

    def composition(self, workspace, *, runtime_factory=None, environment=None):
        state_file = workspace / ".code-mule" / "project-state.json"
        return ProductionCliComposition(
            state_file,
            environment={} if environment is None else environment,
            stdout=StringIO(),
            stderr=StringIO(),
            runtime_factory=runtime_factory,
        )

    def test_start_clean_repo_initializes_and_runs_with_defaults(self):
        runtime_calls = []

        def recording_factory(state):
            runtime_calls.append(state.project.id)
            return runtime_factory_for(self.workspace / ".code-mule" / "project-state.json")(state)

        with chdir(self.workspace):
            composition = self.composition(
                self.workspace,
                runtime_factory=recording_factory,
            )
            result = composition.start("Build a calculator")
        store = JsonProjectStateStore(
            self.workspace / ".code-mule" / "project-state.json"
        )
        state = store.load()
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(state.project.status, ProjectStatus.DONE)
        self.assertEqual(state.project.id, "calculator-app")
        self.assertEqual(state.project.name, "Calculator App")
        self.assertEqual(
            Path(state.project.workspace).resolve(),
            self.workspace.resolve(),
        )
        self.assertEqual(runtime_calls, ["calculator-app"])

    def test_start_existing_project_never_reinitializes(self):
        runtime_calls = []

        def forbidden_runtime(state):
            runtime_calls.append("runtime")
            raise AssertionError("existing project start must not compose runtime")

        with chdir(self.workspace):
            composition = self.composition(
                self.workspace,
                runtime_factory=forbidden_runtime,
            )
            composition.init_project("existing", "Existing", self.workspace)
            state_file = self.workspace / ".code-mule" / "project-state.json"
            before = state_file.read_bytes()
            result = composition.start("Build something else")
            after = state_file.read_bytes()
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(before, after)
        self.assertEqual(runtime_calls, [])
        output = "\n".join(result.output)
        self.assertIn("EXISTING PROJECT", output)
        self.assertIn("Existing Code Mule project detected", output)
        self.assertIn("Next", output)

    def test_start_dirty_repo_is_blocked(self):
        (self.workspace / "uncommitted.txt").write_text("work", encoding="utf-8")
        with chdir(self.workspace):
            composition = self.composition(
                self.workspace,
                runtime_factory=runtime_factory_for(self.workspace / ".code-mule" / "project-state.json"),
            )
            result = composition.start("Build it")
        self.assertEqual(
            result.exit_code,
            CliExitCode.INVALID_PROJECT_STATE,
        )
        output = "\n".join(result.output)
        self.assertIn("WORKSPACE BLOCKED", output)
        self.assertIn("never stashes, resets, or cleans", output)
        self.assertFalse(
            (self.workspace / ".code-mule" / "project-state.json").exists()
        )

    def test_start_repo_without_head_is_blocked(self):
        empty = git_repo(self.root / "empty-repo", commit=False)
        with chdir(empty):
            composition = self.composition(empty)
            result = composition.start("Build it")
        self.assertEqual(
            result.exit_code,
            CliExitCode.INVALID_PROJECT_STATE,
        )
        output = "\n".join(result.output)
        self.assertIn("INITIAL GIT COMMIT REQUIRED", output)
        self.assertIn("does not create Boss commits", output)

    def test_start_outside_git_repo_is_blocked(self):
        plain = self.root / "plain-directory"
        plain.mkdir()
        with chdir(plain):
            composition = self.composition(plain)
            result = composition.start("Build it")
        self.assertEqual(
            result.exit_code,
            CliExitCode.INVALID_PROJECT_STATE,
        )
        output = "\n".join(result.output)
        self.assertIn("GIT REPOSITORY REQUIRED", output)
        self.assertIn("git init", output)

    def test_start_requires_objective_for_new_project(self):
        from code_mule.cli.contracts import CliUsageError

        with chdir(self.workspace):
            composition = self.composition(self.workspace)
            with self.assertRaises(CliUsageError):
                composition.start(None)
        self.assertFalse(
            (self.workspace / ".code-mule" / "project-state.json").exists()
        )

    def test_start_missing_key_fails_before_init_without_runtime_factory(self):
        with chdir(self.workspace):
            composition = self.composition(self.workspace)
            result = composition.start("Build a calculator")
        self.assertEqual(
            result.exit_code,
            CliExitCode.PROVIDER_OR_WORKER_FAILURE,
        )
        output = "\n".join(result.output)
        self.assertIn("MODEL NOT CONFIGURED", output)
        self.assertIn("DEEPSEEK_API_KEY", output)
        self.assertFalse(
            (self.workspace / ".code-mule" / "project-state.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
