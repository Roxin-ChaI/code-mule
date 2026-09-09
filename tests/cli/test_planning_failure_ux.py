from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli import CliExitCode, main
from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain import HumanActionCategory, HumanResolutionStrategy, ProjectStatus
from code_mule.planning import ProjectPlanningService
from code_mule.progress import ConsoleProgressRenderer
from code_mule.recovery import RecoveryMode
from code_mule.recovery.service import RecoveryClassifier
from code_mule.state.store import JsonProjectStateStore
from code_mule.supervisor import (
    SupervisorFailureCategory,
    SupervisorOperation,
)
from planning.test_validation import valid_proposal


class _SafeTypedFailure(RuntimeError):
    failure_category = SupervisorFailureCategory.PROVIDER_AUTHENTICATION
    operation = SupervisorOperation.PLAN
    attempt_count = 1


class _FlakySupervisor:
    def __init__(self):
        self.calls = 0

    def plan(self, request):
        self.calls += 1
        if self.calls == 1:
            raise _SafeTypedFailure("sk-secret raw model response")
        return valid_proposal()


class _NoopExecution:
    def __init__(self):
        self.calls = 0

    def run(self):
        self.calls += 1
        return SimpleNamespace(stop_reason=SimpleNamespace(value="task_limit_reached"))

    def recover(self, plan):
        return self.run()


class _NoopChange:
    pass


class _BrokenPlanning:
    def plan(self, request):
        raise OSError("persistence boundary failed")


class PlanningFailureCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_file = self.root / "state" / "project-state.json"
        self.stdout = StringIO()
        self.stderr = StringIO()
        self.supervisor = _FlakySupervisor()
        self.execution = _NoopExecution()
        self.clock_calls = 0
        self.event_ids = 0
        self.composition = ProductionCliComposition(
            self.state_file,
            environment={},
            stdout=self.stdout,
            stderr=self.stderr,
            runtime_factory=self._runtime,
        )
        self.composition.init_project(
            "planning-ux", "Planning UX", self.workspace
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _clock(self):
        value = datetime(2026, 9, 10, tzinfo=UTC) + timedelta(
            seconds=self.clock_calls
        )
        self.clock_calls += 1
        return value

    def _event_id(self):
        self.event_ids += 1
        return f"planning-event-{self.event_ids}"

    def _runtime(self, state):
        store = JsonProjectStateStore(self.state_file)
        return RuntimeComposition(
            supervisor=self.supervisor,
            worker_service=object(),
            planning=ProjectPlanningService(
                store=store,
                supervisor=self.supervisor,
                clock=self._clock,
                plan_id_factory=lambda: "PLAN-1",
                event_id_factory=self._event_id,
            ),
            execution=self.execution,
            change_execution=_NoopChange(),
            renderer=ConsoleProgressRenderer(self.stderr),
        )

    def _invoke(self, arguments):
        return main(
            arguments,
            composition_factory=lambda *_: self.composition,
            environment={},
            stdout=self.stdout,
            stderr=self.stderr,
        )

    def test_expected_planning_gate_is_action_required_not_unexpected_error(self):
        code = self._invoke(["run", "--objective", "Build it"])
        state = JsonProjectStateStore(self.state_file).load()
        output = self.stdout.getvalue()
        errors = self.stderr.getvalue()

        self.assertEqual(code, CliExitCode.HUMAN_ACTION_REQUIRED)
        self.assertIn("ACTION REQUIRED", output)
        self.assertNotIn("UNEXPECTED ERROR", output + errors)
        self.assertIs(state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(len(state.human_actions), 1)
        self.assertIs(
            state.human_actions[0].category,
            HumanActionCategory.SUPERVISOR_FAILURE,
        )
        self.assertEqual(state.plans, ())
        self.assertEqual(state.revisions, ())
        self.assertEqual(self.execution.calls, 0)
        self.assertNotIn("sk-secret", output + errors + str(state.events))
        self.assertNotIn("raw model response", output + errors + str(state.events))

    def test_inspect_lists_typed_diagnostics_and_actual_strategies(self):
        self._invoke(["run", "--objective", "Build it"])
        inspected = "\n".join(self.composition.inspect(verbose=True).output)

        self.assertIn("SUPERVISOR PLANNING FAILURE", inspected)
        self.assertIn("Stage        Planning", inspected)
        self.assertIn("Failure      Provider authentication", inspected)
        self.assertIn("Retryable    No", inspected)
        self.assertIn("Plan created No", inspected)
        self.assertIn("Worker started No", inspected)
        self.assertIn("Allowed strategies:", inspected)
        self.assertIn("retry_planning", inspected)
        self.assertIn("fail_project", inspected)
        self.assertIn("acknowledge", inspected)
        self.assertNotIn("sk-secret", inspected)

    def test_explicit_retry_reuses_project_and_materializes_once(self):
        self._invoke(["run", "--objective", "Build it"])
        store = JsonProjectStateStore(self.state_file)
        action = store.load().human_actions[0]

        resolved = self.composition.resolve(
            action.id, HumanResolutionStrategy.RETRY_PLANNING
        )
        staged = store.load()
        self.assertIn("code-mule recover", "\n".join(resolved.output))
        self.assertIs(
            RecoveryClassifier().classify(staged).recovery_mode,
            RecoveryMode.FRESH_PLANNING,
        )
        self.assertEqual(staged.plans, ())
        self.assertEqual(staged.revisions, ())

        result = self.composition.recover()
        final = store.load()

        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(self.supervisor.calls, 2)
        self.assertEqual(len(final.plans), 1)
        self.assertEqual(len(final.revisions), 1)
        self.assertEqual(final.revisions[0].revision_number, 1)
        self.assertEqual(final.project.id, "planning-ux")

    def test_unpersisted_runtime_failure_is_not_misreported_as_action_gate(self):
        self.composition._runtime_factory = lambda state: RuntimeComposition(
            supervisor=object(),
            worker_service=object(),
            planning=_BrokenPlanning(),
            execution=self.execution,
            change_execution=_NoopChange(),
            renderer=ConsoleProgressRenderer(self.stderr),
        )

        code = self._invoke(["run", "--objective", "Build it"])
        state = JsonProjectStateStore(self.state_file).load()

        self.assertEqual(code, CliExitCode.PROVIDER_OR_WORKER_FAILURE)
        self.assertIn("UNEXPECTED ERROR", self.stderr.getvalue())
        self.assertNotIn("ACTION REQUIRED", self.stdout.getvalue())
        self.assertIs(state.project.status, ProjectStatus.IDLE)
        self.assertEqual(state.human_actions, ())


if __name__ == "__main__":
    unittest.main()
