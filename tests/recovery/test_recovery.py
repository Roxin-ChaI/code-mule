from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from code_mule.domain import HumanActionCategory, HumanActionStatus, ProjectStatus, TaskStatus
from code_mule.domain.models import HumanAction, WorkerInputDetails
from code_mule.recovery import (
    BoundaryRecoverability,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionStopBoundary,
    ExecutionPhase,
    ExecutionStopReason,
    RecoveryMode,
    SafePoint,
    SafePointKind,
    WorkerTerminalState,
)
from code_mule.recovery.service import RecoveryClassifier, RecoveryPreflightError
from code_mule.state.serialization import deserialize_project_state, serialize_project_state
from runtime.test_cycle import FakeGitDelivery, FakeStore, FakeSupervisor, FakeWorkerSession, build_cycle, cycle_state, review
from code_mule.domain import SupervisorDecisionType
from state import make_project_state


NOW = datetime(2026, 9, 7, tzinfo=UTC)


class RecoveryContractTests(unittest.TestCase):
    def test_boundary_and_safe_point_round_trip(self):
        state = replace(
            make_project_state(),
            latest_safe_point=SafePoint(SafePointKind.TASK_WORKER_COMPLETED, NOW, "task-1", 3, "a" * 40),
            latest_execution_stop=ExecutionStopBoundary(
                ExecutionStopReason.WORKER_INPUT, ExecutionPhase.WORKER,
                SafePointKind.HUMAN_GATE, BoundaryRecoverability.RECOVERABLE,
                True, WorkerTerminalState.COMPLETED, True, NOW,
                "task-1", 3, "a" * 40,
            ),
        )
        self.assertEqual(deserialize_project_state(serialize_project_state(state)), state)

    def test_attempt_lifecycle_round_trip(self):
        attempt = ExecutionAttempt(
            "task-1", 3, ExecutionAttemptStatus.REPORT_PERSISTED, NOW,
            "thread-1", "turn-1", "a" * 40, NOW, None, True,
        )
        state = replace(make_project_state(), execution_attempts=(attempt,))
        self.assertEqual(deserialize_project_state(serialize_project_state(state)), state)

    def test_v11_migration_is_conservative_for_running_state(self):
        payload = serialize_project_state(make_project_state())
        payload["schema_version"] = 11
        payload.pop("latest_execution_stop")
        payload.pop("latest_safe_point")
        payload.pop("execution_attempts")
        restored = deserialize_project_state(payload)
        self.assertIs(restored.latest_safe_point.kind, SafePointKind.UNCERTAIN)
        self.assertIsNone(restored.latest_execution_stop)
        self.assertEqual(restored.execution_attempts, ())

    def test_v11_idle_migration_records_only_project_idle(self):
        state = make_project_state()
        state = replace(state, project=replace(state.project, status=ProjectStatus.IDLE, active_plan_id=None, current_task_id=None), plans=(), milestones=(), tasks=())
        payload = serialize_project_state(state)
        payload["schema_version"] = 11
        for key in ("latest_execution_stop", "latest_safe_point", "execution_attempts"):
            payload.pop(key)
        restored = deserialize_project_state(payload)
        self.assertIs(restored.latest_safe_point.kind, SafePointKind.PROJECT_IDLE)

    def test_invalid_attempt_and_boundary_contracts_fail_closed(self):
        with self.assertRaises(ValueError):
            ExecutionAttempt("task-1", 0, ExecutionAttemptStatus.PREPARED, NOW)
        with self.assertRaises(ValueError):
            ExecutionStopBoundary(
                ExecutionStopReason.WORKER_FAILED, ExecutionPhase.WORKER,
                SafePointKind.UNCERTAIN, BoundaryRecoverability.UNCERTAIN,
                False, WorkerTerminalState.FAILED, False, NOW,
            )


class RecoveryClassificationTests(unittest.TestCase):
    def setUp(self):
        self.classifier = RecoveryClassifier()
        self.state = replace(
            make_project_state(),
            project=replace(make_project_state().project, current_task_id=None),
            tasks=(replace(make_project_state().tasks[0], status=TaskStatus.PENDING),),
            latest_safe_point=SafePoint(SafePointKind.PLAN_MATERIALIZED, NOW),
        )

    def test_materialized_plan_resumes_without_planning(self):
        plan = self.classifier.classify(self.state)
        self.assertIs(plan.recovery_mode, RecoveryMode.RESUME_PLAN)
        self.assertFalse(plan.fresh_worker_required)

    def test_selected_task_without_worker_dispatches_fresh_worker(self):
        state = replace(self.state, project=replace(self.state.project, current_task_id="task-1"), tasks=(replace(self.state.tasks[0], status=TaskStatus.IN_PROGRESS),), latest_safe_point=SafePoint(SafePointKind.TASK_READY, NOW, "task-1"))
        plan = self.classifier.classify(state)
        self.assertIs(plan.recovery_mode, RecoveryMode.DISPATCH_FRESH_WORKER)

    def test_prepared_attempt_is_recoverable_with_workspace_validation(self):
        state = self._current(ExecutionAttemptStatus.PREPARED)
        plan = self.classifier.classify(state)
        self.assertTrue(plan.requires_workspace_validation)
        self.assertTrue(plan.fresh_worker_required)

    def test_report_persisted_continues_without_worker(self):
        state = self._current(ExecutionAttemptStatus.REPORT_PERSISTED)
        plan = self.classifier.classify(state)
        self.assertIs(plan.recovery_mode, RecoveryMode.CONTINUE_AFTER_REPORT)
        self.assertFalse(plan.fresh_worker_required)

    def test_worker_started_without_terminal_is_uncertain(self):
        plan = self.classifier.classify(self._current(ExecutionAttemptStatus.WORKER_STARTED))
        self.assertIs(plan.recovery_mode, RecoveryMode.BLOCKED)
        self.assertFalse(plan.automatic_resume_allowed)

    def test_interrupted_worker_never_auto_retries(self):
        plan = self.classifier.classify(self._current(ExecutionAttemptStatus.INTERRUPTED))
        self.assertIs(plan.recoverability, BoundaryRecoverability.UNCERTAIN)
        self.assertFalse(plan.fresh_worker_required)

    def test_failed_worker_never_auto_retries(self):
        plan = self.classifier.classify(self._current(ExecutionAttemptStatus.FAILED))
        self.assertIs(plan.recovery_mode, RecoveryMode.BLOCKED)

    def test_answered_worker_input_uses_fresh_session_same_task(self):
        details = WorkerInputDetails("worker/report", "report-1", "Storage?", (), 2, "a" * 40, ("app.js",), "localStorage")
        action = HumanAction("action-1", "project-1", "task-1", HumanActionCategory.WORKER_INPUT, "Input", "Answer", "Risk", HumanActionStatus.RESOLVED, NOW, NOW, details)
        state = replace(self.state, human_actions=(action,))
        plan = self.classifier.classify(state)
        self.assertIs(plan.recovery_mode, RecoveryMode.CONTINUE_AFTER_INPUT)
        self.assertEqual(plan.task_id, "task-1")

    def test_pending_action_is_never_resolved_by_preflight(self):
        action = HumanAction("action-1", "project-1", None, HumanActionCategory.RECOVERY_UNCERTAIN, "Inspect", "Inspect", "Risk", HumanActionStatus.PENDING, NOW)
        state = replace(self.state, project=replace(self.state.project, status=ProjectStatus.HUMAN_REQUIRED), human_actions=(action,))
        before = serialize_project_state(state)
        plan = self.classifier.classify(state)
        self.assertIs(plan.recovery_mode, RecoveryMode.BLOCKED)
        self.assertEqual(serialize_project_state(state), before)

    def test_planning_before_materialization_requires_fresh_planning(self):
        state = replace(self.state, project=replace(self.state.project, status=ProjectStatus.PLANNING, active_plan_id=None), plans=(), milestones=(), tasks=())
        self.assertIs(self.classifier.classify(state).recovery_mode, RecoveryMode.FRESH_PLANNING)

    def test_planning_after_materialization_reuses_plan(self):
        state = replace(self.state, project=replace(self.state.project, status=ProjectStatus.PLANNING))
        self.assertIs(self.classifier.classify(state).recovery_mode, RecoveryMode.RESUME_PLAN)

    def test_terminal_project_has_no_recovery(self):
        state = replace(self.state, project=replace(self.state.project, status=ProjectStatus.DONE))
        self.assertIs(self.classifier.classify(state).recovery_mode, RecoveryMode.NOT_NEEDED)

    def test_pause_keeps_resume_separate_from_recovery(self):
        state = replace(self.state, project=replace(self.state.project, status=ProjectStatus.PAUSED_BY_BOSS))
        self.assertEqual(self.classifier.classify(state).next_command, "code-mule resume")

    def test_classifier_does_not_change_plan_tasks_or_attempts(self):
        before = serialize_project_state(self.state)
        self.classifier.classify(self.state)
        self.assertEqual(serialize_project_state(self.state), before)

    def _current(self, status):
        task = replace(self.state.tasks[0], status=TaskStatus.IN_PROGRESS)
        attempt = ExecutionAttempt("task-1", 1, status, NOW, "thread-1" if status is not ExecutionAttemptStatus.PREPARED else None, None, "a" * 40, NOW if status is not ExecutionAttemptStatus.PREPARED else None)
        return replace(self.state, project=replace(self.state.project, current_task_id="task-1"), tasks=(task,), execution_attempts=(attempt,))


class WorkspaceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(("git", "init", "-q"), cwd=self.root, check=True)
        subprocess.run(("git", "config", "user.name", "Recovery Test"), cwd=self.root, check=True)
        subprocess.run(("git", "config", "user.email", "recovery@example.invalid"), cwd=self.root, check=True)
        (self.root / "README.md").write_text("baseline\n")
        subprocess.run(("git", "add", "README.md"), cwd=self.root, check=True)
        subprocess.run(("git", "commit", "-qm", "initial"), cwd=self.root, check=True)
        self.head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.root, text=True, capture_output=True, check=True).stdout.strip()

    def tearDown(self):
        self.temp.cleanup()

    def test_head_drift_blocks_recovery(self):
        state = self._state(self.head[:-1] + ("0" if self.head[-1] != "0" else "1"))
        with self.assertRaisesRegex(RecoveryPreflightError, "HEAD drifted"):
            RecoveryClassifier().classify(state, validate_workspace=True)

    def test_staged_path_blocks_recovery(self):
        (self.root / "other.txt").write_text("other\n")
        subprocess.run(("git", "add", "other.txt"), cwd=self.root, check=True)
        with self.assertRaisesRegex(RecoveryPreflightError, "staged"):
            RecoveryClassifier().classify(self._state(self.head), validate_workspace=True)

    def _state(self, head):
        source = make_project_state()
        task = replace(source.tasks[0], status=TaskStatus.IN_PROGRESS)
        attempt = ExecutionAttempt("task-1", 1, ExecutionAttemptStatus.PREPARED, NOW, baseline_head=head)
        return replace(source, project=replace(source.project, workspace=str(self.root)), tasks=(task,), execution_attempts=(attempt,), latest_safe_point=SafePoint(SafePointKind.TASK_BASELINE_CAPTURED, NOW, "task-1", 1, head))


class ProcessRestartRecoveryTests(unittest.TestCase):
    def test_restart_before_worker_reuses_same_plan_and_task(self):
        source = make_project_state()
        task = replace(source.tasks[0], status=TaskStatus.IN_PROGRESS)
        state = replace(source, project=replace(source.project, current_task_id=task.id), tasks=(task,), execution_attempts=(), latest_safe_point=SafePoint(SafePointKind.TASK_READY, NOW, task.id))
        restarted = deserialize_project_state(serialize_project_state(state))
        plan = RecoveryClassifier().classify(restarted)
        self.assertIs(plan.recovery_mode, RecoveryMode.DISPATCH_FRESH_WORKER)
        self.assertEqual(restarted.project.active_plan_id, source.project.active_plan_id)
        self.assertEqual(tuple(item.id for item in restarted.tasks), (task.id,))

    def test_restart_after_report_never_invokes_worker_again(self):
        delivery = FakeGitDelivery()
        store = FakeStore(cycle_state(), fail_on_save=5)
        first, request, _, first_session, _, _ = build_cycle(
            store=store, git_delivery=delivery
        )
        with self.assertRaises(OSError):
            first.execute(request)
        self.assertEqual(len(first_session.requests), 1)
        self.assertIs(store.current.execution_attempts[-1].status, ExecutionAttemptStatus.REPORT_PERSISTED)
        restarted = deserialize_project_state(serialize_project_state(store.current))
        store.current = restarted
        store.fail_on_save = None
        unused_worker = FakeWorkerSession()
        supervisor = FakeSupervisor([review(SupervisorDecisionType.CONTINUE)])
        recovered, recovery_request, _, _, _, sessions = build_cycle(
            store=store,
            session=unused_worker,
            supervisor=supervisor,
            git_delivery=delivery,
        )
        outcome = recovered.resume_after_report(recovery_request)
        self.assertFalse(outcome.human_action_required)
        self.assertEqual(sessions, [])
        self.assertEqual(unused_worker.requests, [])
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)

    def test_restart_after_worker_started_remains_blocked(self):
        source = make_project_state()
        task = replace(source.tasks[0], status=TaskStatus.IN_PROGRESS)
        attempt = ExecutionAttempt("task-1", 1, ExecutionAttemptStatus.WORKER_STARTED, NOW, "thread-1", baseline_head="a" * 40)
        state = replace(source, project=replace(source.project, current_task_id=task.id), tasks=(task,), execution_attempts=(attempt,))
        restarted = deserialize_project_state(serialize_project_state(state))
        self.assertIs(RecoveryClassifier().classify(restarted).recovery_mode, RecoveryMode.BLOCKED)

    def test_restart_after_materialization_does_not_plan_again(self):
        restarted = deserialize_project_state(serialize_project_state(make_project_state()))
        restarted = replace(restarted, project=replace(restarted.project, current_task_id=None), tasks=(replace(restarted.tasks[0], status=TaskStatus.PENDING),), latest_safe_point=SafePoint(SafePointKind.PLAN_MATERIALIZED, NOW))
        plan_ids = tuple(item.id for item in restarted.plans)
        recovery = RecoveryClassifier().classify(restarted)
        self.assertIs(recovery.recovery_mode, RecoveryMode.RESUME_PLAN)
        self.assertEqual(tuple(item.id for item in restarted.plans), plan_ids)


if __name__ == "__main__":
    unittest.main()
