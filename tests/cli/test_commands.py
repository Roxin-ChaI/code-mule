from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli import (
    CliCommandResult,
    CliExitCode,
    CliProjectAlreadyRunning,
    InvalidCliProjectState,
    main,
)
from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain.enums import (
    HumanActionCategory,
    HumanActionStatus,
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import Milestone, Plan, Task
from code_mule.human import request_human_action
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
    def status(self, *values): return self._call("status", *values)
    def ask(self, *values): return self._call("ask", *values)
    def change(self, *values): return self._call("change", *values)
    def apply_change(self, *values): return self._call("apply_change", *values)
    def pause(self, *values): return self._call("pause", *values)
    def resume(self, *values): return self._call("resume", *values)
    def stop(self, *values): return self._call("stop", *values)
    def inspect(self, *values): return self._call("inspect", *values)
    def approve(self, *values): return self._call("approve", *values)
    def reject(self, *values): return self._call("reject", *values)
    def resolve(self, *values): return self._call("resolve", *values)
    def chat(self, *values): return self._call("chat", *values)


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


class _InterruptingExecution:
    def __init__(self, store):
        self.store = store

    def run(self):
        state = self.store.load()
        task = replace(state.tasks[0], status=TaskStatus.IN_PROGRESS)
        self.store.save(
            replace(
                state,
                project=replace(state.project, current_task_id=task.id),
                tasks=(task,),
            )
        )
        raise KeyboardInterrupt


class _BoundaryInterruptingExecution:
    def run(self):
        raise KeyboardInterrupt


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

    def test_stop_is_keyless_typed_and_idempotent(self):
        composition = self.init()
        first = composition.stop()
        state = JsonProjectStateStore(self.state_file).load()
        self.assertEqual(first.exit_code, CliExitCode.SUCCESS)
        self.assertIn("PROJECT CANCELLED", first.output)
        self.assertIs(state.project.status, ProjectStatus.CANCELLED)
        self.assertIn("No rollback was performed.", first.output)
        before = self.state_file.read_bytes()
        second = composition.stop()
        self.assertEqual(second.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(self.state_file.read_bytes(), before)

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

    def test_live_owner_blocks_execution_but_allows_status_and_change(self):
        runtime_calls = []

        def runtime_factory(state):
            runtime_calls.append(state.project.id)
            return self.runtime_factory(state)

        composition = self.composition(runtime_factory)
        self.init(composition)
        store = JsonProjectStateStore(self.state_file)
        state = store.load()
        now = datetime.now(UTC)
        plan = Plan(
            "P1",
            state.project.id,
            1,
            PlanStatus.ACTIVE,
            (),
            ("M1",),
            now,
        )
        milestone = Milestone("M1", plan.id, "M", "active", ("T1",))
        task = Task(
            "T1",
            "M1",
            "T",
            "D",
            TaskStatus.PENDING,
            (),
            ("done",),
            0,
            now,
            now,
        )
        store.save(
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
        owner = composition._ownership().acquire()
        try:
            self.assertEqual(composition.status().exit_code, CliExitCode.SUCCESS)
            self.assertEqual(
                composition.change("Add export").exit_code,
                CliExitCode.SUCCESS,
            )
            with self.assertRaises(CliProjectAlreadyRunning):
                composition.apply_change()
            self.assertEqual(runtime_calls, [])
        finally:
            owner.close()

    def test_live_owner_allows_boss_pause_without_starting_runtime(self):
        composition = self.init()
        store = JsonProjectStateStore(self.state_file)
        state = store.load()
        now = datetime.now(UTC)
        plan = Plan(
            "P1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), now
        )
        milestone = Milestone("M1", plan.id, "M", "active", ("T1",))
        task = Task(
            "T1", "M1", "T", "D", TaskStatus.PENDING, (), ("done",), 0,
            now, now,
        )
        store.save(
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
        owner = composition._ownership().acquire()
        try:
            self.assertEqual(composition.pause().exit_code, CliExitCode.SUCCESS)
            self.assertIs(
                store.load().project.status,
                ProjectStatus.PAUSED_BY_BOSS,
            )
            with self.assertRaises(CliProjectAlreadyRunning):
                composition.resume()
            self.assertIs(
                store.load().project.status,
                ProjectStatus.PAUSED_BY_BOSS,
            )
        finally:
            owner.close()

    def test_keyboard_interrupt_releases_owner_and_requires_safe_recovery(self):
        def runtime_factory(state):
            store = JsonProjectStateStore(self.state_file)
            return RuntimeComposition(
                supervisor=object(),
                worker_service=object(),
                planning=_FakePlanning(store),
                execution=_InterruptingExecution(store),
                change_execution=_FakeChangeExecution(store),
                renderer=ConsoleProgressRenderer(self.stderr),
            )

        composition = self.composition(runtime_factory)
        self.init(composition)
        with self.assertRaisesRegex(Exception, "interrupted during an active Task"):
            composition.run("Build it")

        final = JsonProjectStateStore(self.state_file).load()
        self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(final.execution_leases[-1].status.value, "released")
        self.assertEqual(
            final.human_actions[-1].category,
            HumanActionCategory.RECOVERY_UNCERTAIN,
        )

    def test_keyboard_interrupt_at_task_boundary_releases_cleanly(self):
        def runtime_factory(state):
            store = JsonProjectStateStore(self.state_file)
            return RuntimeComposition(
                supervisor=object(),
                worker_service=object(),
                planning=_FakePlanning(store),
                execution=_BoundaryInterruptingExecution(),
                change_execution=_FakeChangeExecution(store),
                renderer=ConsoleProgressRenderer(self.stderr),
            )

        composition = self.composition(runtime_factory)
        self.init(composition)
        with self.assertRaisesRegex(Exception, "safe boundary"):
            composition.run("Build it")

        final = JsonProjectStateStore(self.state_file).load()
        self.assertIs(final.project.status, ProjectStatus.RUNNING)
        self.assertIsNone(final.project.current_task_id)
        self.assertEqual(final.human_actions, ())
        self.assertEqual(final.execution_leases[-1].status.value, "released")

    def test_status_and_ask_are_read_only_without_api_key(self):
        composition = self.init()
        before = self.state_file.read_bytes()
        status = composition.status()
        self.assertEqual(status.exit_code, CliExitCode.SUCCESS)
        default = "\n".join(status.output)
        self.assertIn("PROJECT\nProject", default)
        self.assertIn("Status      Ready", default)
        self.assertNotIn("project_id:", default)
        verbose = "\n".join(composition.status(verbose=True).output)
        self.assertIn("project_id: project-1", verbose)
        self.assertIn("project_status: idle", verbose)
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
        self.assertIn("CHANGE REQUESTED", submitted.output)
        self.assertIn('"Add multiply"', submitted.output)
        self.assertIn("Current task will finish safely", "\n".join(submitted.output))
        self.assertEqual(store.load().project.status, ProjectStatus.CHANGE_REQUESTED)
        applied = composition.apply_change()
        self.assertEqual(applied.exit_code, CliExitCode.SUCCESS)
        self.assertIn("REPLANNING", applied.output)
        self.assertIn("✓ Impact analysis completed", applied.output)
        self.assertIn("Execution resumed.", applied.output)
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

    def test_inspect_is_read_only_and_approval_is_action_scoped(self):
        composition = self.init()
        store = JsonProjectStateStore(self.state_file)
        state = store.load()
        ids = iter(("source", "requested"))
        gated = request_human_action(
            replace(state, project=replace(state.project, status=ProjectStatus.RUNNING)),
            category=HumanActionCategory.WORKER_APPROVAL,
            summary="Approve one Worker operation",
            requested_action="Review the operation",
            risk="External side effect",
            task_id=None,
            operation_time=datetime.now(UTC),
            action_id="action-123",
            event_id_factory=lambda: next(ids),
            source_event_types=("task.human_required",),
        )
        store.save(gated)
        before = self.state_file.read_bytes()
        inspected = composition.inspect(verbose=True)
        self.assertIn("ACTION REQUIRED", inspected.output)
        self.assertIn("action_id: action-123", inspected.output)
        self.assertEqual(self.state_file.read_bytes(), before)
        with self.assertRaisesRegex(Exception, "unknown"):
            composition.approve("future-action")
        approved = composition.approve("action-123")
        self.assertIn("ACTION APPROVED", approved.output)
        self.assertIs(
            store.load().human_actions[0].status, HumanActionStatus.APPROVED
        )
        with self.assertRaisesRegex(Exception, "already closed"):
            composition.approve("action-123")

    def test_reject_never_resumes_project(self):
        composition = self.init()
        store = JsonProjectStateStore(self.state_file)
        state = store.load()
        ids = iter(("source", "requested"))
        store.save(
            request_human_action(
                replace(state, project=replace(state.project, status=ProjectStatus.RUNNING)),
                category=HumanActionCategory.EXTERNAL_SIDE_EFFECT,
                summary="Push requested",
                requested_action="Review push",
                risk="Remote mutation",
                task_id=None,
                operation_time=datetime.now(UTC),
                action_id="action-reject",
                event_id_factory=lambda: next(ids),
                source_event_types=("task.human_required",),
            )
        )
        composition.reject("action-reject")
        updated = store.load()
        self.assertIs(updated.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.REJECTED)


class CliProcessBoundaryTests(unittest.TestCase):
    def invoke(self, argv, commands, environment=None, stdin=None):
        output = StringIO(); errors = StringIO()
        code = main(
            argv,
            composition_factory=lambda *_: commands,
            environment={} if environment is None else environment,
            stdout=output,
            stderr=errors,
            stdin=stdin,
        )
        return code, output.getvalue(), errors.getvalue()

    def test_dispatches_commands_and_prints_structured_output(self):
        commands = _FakeCommands()
        code, output, errors = self.invoke(["status"], commands)
        self.assertEqual(code, 0)
        self.assertEqual(output, "ok\n")
        self.assertEqual(errors, "")
        self.assertEqual(commands.calls, [("status", (False,))])

    def test_dispatches_human_resolution_commands(self):
        cases = (
            (["stop"], "stop"),
            (["inspect", "--verbose"], "inspect"),
            (["approve", "action-1"], "approve"),
            (["reject", "action-1"], "reject"),
            (["resolve", "action-1", "--strategy", "acknowledge"], "resolve"),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                commands = _FakeCommands()
                code, _, _ = self.invoke(argv, commands)
                self.assertEqual(code, 0)
                self.assertEqual(commands.calls[0][0], expected)

    def test_dispatches_chat_with_one_persistent_input_stream(self):
        commands = _FakeCommands()
        input_stream = StringIO("状态\n")
        code, _, _ = self.invoke(
            ["chat", "--verbose"],
            commands,
            stdin=input_stream,
        )
        self.assertEqual(code, 0)
        self.assertEqual(commands.calls, [("chat", (input_stream, True))])

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

    def test_typed_error_names_layer_and_gives_next_step(self):
        commands = _FakeCommands(
            error=InvalidCliProjectState("planning state is not recoverable")
        )
        code, _, errors = self.invoke(["status"], commands)
        self.assertEqual(code, 3)
        self.assertIn("PROJECT STATE ERROR", errors)
        self.assertIn("planning state is not recoverable", errors)
        self.assertIn("Next:\n  code-mule status", errors)
        self.assertNotIn("Traceback", errors)

    def test_live_owner_error_has_typed_exit_and_safe_message(self):
        commands = _FakeCommands(
            error=CliProjectAlreadyRunning("Another execution owner is active.")
        )
        code, _, errors = self.invoke(["run"], commands)
        self.assertEqual(code, 3)
        self.assertIn("PROJECT ALREADY RUNNING", errors)
        self.assertIn("code-mule status", errors)


if __name__ == "__main__":
    unittest.main()
