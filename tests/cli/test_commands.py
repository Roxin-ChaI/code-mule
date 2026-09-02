from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli import CliCommandResult, CliExitCode, main
from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain.enums import (
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import Milestone, Plan, Task
from code_mule.progress import ConsoleProgressRenderer
from code_mule.runtime import ProjectExecutionStopReason
from code_mule.state.store import JsonProjectStateStore


class _FakeCommands:
    def __init__(self, result=None, error=None):
        self.result = result or CliCommandResult(CliExitCode.SUCCESS, ("ok",))
        self.error = error
        self.calls = []

    def _call(self, name, *values):
        self.calls.append((name, values))
        if self.error is not None:
            raise self.error
        return self.result

    def init_project(self, *values): return self._call("init", *values)
    def run(self, *values): return self._call("run", *values)
    def status(self): return self._call("status")
    def ask(self, *values): return self._call("ask", *values)
    def change(self, *values): return self._call("change", *values)
    def apply_change(self): return self._call("apply_change")
    def pause(self): return self._call("pause")
    def resume(self): return self._call("resume")


class _FakePlanning:
    def __init__(self, store): self.store = store
    def plan(self, request):
        state = self.store.load()
        now = datetime.now(UTC)
        plan = Plan("PLAN-1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), now)
        milestone = Milestone("M1", plan.id, "Work", "active", ("T1",))
        task = Task("T1", "M1", "Work", "Do work", TaskStatus.PENDING, (), ("done",), 0, now, now)
        self.store.save(replace(state, project=replace(state.project, status=ProjectStatus.RUNNING, active_plan_id=plan.id), plans=(plan,), milestones=(milestone,), tasks=(task,)))
        return SimpleNamespace(ready_for_execution=True, plan_id=plan.id)


class _FakeExecution:
    def __init__(self, store): self.store = store; self.calls = 0
    def run(self):
        self.calls += 1
        state = self.store.load()
        tasks = tuple(replace(task, status=TaskStatus.COMPLETED) for task in state.tasks)
        plans = tuple(replace(plan, status=PlanStatus.COMPLETED) for plan in state.plans)
        self.store.save(replace(state, project=replace(state.project, status=ProjectStatus.DONE, current_task_id=None), tasks=tasks, plans=plans))
        return SimpleNamespace(stop_reason=ProjectExecutionStopReason.PLAN_COMPLETED, human_action_required=False)


class _FakeChangeExecution:
    def __init__(self, store): self.store = store
    def apply_and_resume(self, request):
        state = self.store.load()
        self.store.save(replace(state, project=replace(state.project, status=ProjectStatus.DONE)))
        return SimpleNamespace(
            replanning=SimpleNamespace(plan_id="PLAN-2", plan_version=2),
            human_action_required=False,
        )


class ProductionCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_file = self.root / "state" / "project.json"
        self.stdout = StringIO()
        self.stderr = StringIO()

    def tearDown(self): self.temporary.cleanup()

    def composition(self, runtime_factory=None):
        return ProductionCliComposition(
            self.state_file,
            environment={},
            stdout=self.stdout,
            stderr=self.stderr,
            runtime_factory=runtime_factory,
        )

    def init(self, composition=None):
        value = composition or self.composition()
        value.init_project("project-1", "Project", self.workspace)
        return value

    def runtime_factory(self, state):
        store = JsonProjectStateStore(self.state_file)
        renderer = ConsoleProgressRenderer(self.stderr)
        execution = _FakeExecution(store)
        return RuntimeComposition(
            supervisor=object(),
            worker_service=object(),
            planning=_FakePlanning(store),
            execution=execution,
            change_execution=_FakeChangeExecution(store),
            renderer=renderer,
        )

    def test_init_success_and_existing_file_rejected(self):
        composition = self.init()
        state = JsonProjectStateStore(self.state_file).load()
        self.assertEqual(state.project.status, ProjectStatus.IDLE)
        self.assertEqual(state.project.workspace, str(self.workspace.resolve()))
        with self.assertRaisesRegex(Exception, "already exists"):
            composition.init_project("other", "Other", self.workspace)

    def test_run_new_and_existing_running_project(self):
        composition = self.composition(self.runtime_factory)
        self.init(composition)
        result = composition.run("Build it")
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(JsonProjectStateStore(self.state_file).load().project.status, ProjectStatus.DONE)

        store = JsonProjectStateStore(self.state_file)
        state = store.load()
        store.save(replace(state, project=replace(state.project, status=ProjectStatus.RUNNING), plans=tuple(replace(plan, status=PlanStatus.ACTIVE) for plan in state.plans), tasks=tuple(replace(task, status=TaskStatus.PENDING) for task in state.tasks)))
        result = composition.run(None)
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(store.load().project.status, ProjectStatus.DONE)

    def test_status_and_ask_are_read_only_without_api_key(self):
        composition = self.init()
        before = self.state_file.read_bytes()
        self.assertEqual(composition.status().exit_code, CliExitCode.SUCCESS)
        self.assertEqual(composition.ask("What remains?").exit_code, CliExitCode.SUCCESS)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_change_submission_and_apply(self):
        composition = self.composition(self.runtime_factory)
        self.init(composition)
        store = JsonProjectStateStore(self.state_file)
        state = store.load()
        store.save(replace(state, project=replace(state.project, status=ProjectStatus.RUNNING)))
        submitted = composition.change("Add multiply")
        self.assertEqual(submitted.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(store.load().project.status, ProjectStatus.CHANGE_REQUESTED)
        applied = composition.apply_change()
        self.assertEqual(applied.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(store.load().project.status, ProjectStatus.DONE)

    def test_pause_resume_and_human_required_gate(self):
        composition = self.init()
        store = JsonProjectStateStore(self.state_file)
        now = datetime.now(UTC)
        state = store.load()
        plan = Plan("P1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), now)
        milestone = Milestone("M1", plan.id, "M", "active", ("T1",))
        task = Task("T1", "M1", "T", "D", TaskStatus.PENDING, (), ("done",), 0, now, now)
        store.save(replace(state, project=replace(state.project, status=ProjectStatus.RUNNING, active_plan_id=plan.id), plans=(plan,), milestones=(milestone,), tasks=(task,)))
        self.assertEqual(composition.pause().exit_code, CliExitCode.SUCCESS)
        self.assertEqual(composition.resume().exit_code, CliExitCode.SUCCESS)
        state = store.load()
        store.save(replace(state, project=replace(state.project, status=ProjectStatus.HUMAN_REQUIRED)))
        with self.assertRaisesRegex(Exception, "cannot bypass"):
            composition.resume()

    def test_missing_key_only_blocks_model_dependent_command(self):
        composition = self.init()
        self.assertEqual(composition.status().exit_code, CliExitCode.SUCCESS)
        with self.assertRaisesRegex(Exception, "DEEPSEEK_API_KEY"):
            composition.run("Build it")


class CliProcessBoundaryTests(unittest.TestCase):
    def invoke(self, argv, commands, environment=None):
        output = StringIO(); errors = StringIO()
        code = main(
            argv,
            composition_factory=lambda *_: commands,
            environment={} if environment is None else environment,
            stdout=output,
            stderr=errors,
        )
        return code, output.getvalue(), errors.getvalue()

    def test_dispatches_commands_and_prints_structured_output(self):
        commands = _FakeCommands()
        code, output, errors = self.invoke(["status"], commands)
        self.assertEqual(code, 0)
        self.assertEqual(output, "ok\n")
        self.assertEqual(errors, "")
        self.assertEqual(commands.calls, [("status", ())])

    def test_invalid_change_shape_uses_usage_exit_code(self):
        code, _, errors = self.invoke(["change"], _FakeCommands())
        self.assertEqual(code, 2)
        self.assertIn("requires a request or --apply", errors)

    def test_non_debug_and_debug_errors_never_leak_secret(self):
        secret = "sk-secret-value"
        commands = _FakeCommands(error=RuntimeError(secret))
        code, _, errors = self.invoke(["status"], commands)
        self.assertEqual(code, 5)
        self.assertNotIn(secret, errors)
        self.assertNotIn("Traceback", errors)
        code, _, errors = self.invoke(["--debug", "status"], commands)
        self.assertEqual(code, 5)
        self.assertIn("Traceback", errors)
        self.assertNotIn(secret, errors)


if __name__ == "__main__":
    unittest.main()
