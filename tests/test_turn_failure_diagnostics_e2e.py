"""Fake App Server through production session, TaskCycle, state, lease and inspect."""

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain.enums import ProjectStatus, TaskStatus, HumanActionCategory, HumanActionStatus
from code_mule.execution import ExecutionLeaseStatus
from code_mule.git_delivery import GitDeliveryService
from code_mule.runtime import ProjectExecutionService, ProjectExecutionConfig, TaskCycleService, TaskCycleConfig, TaskPromptBuilder
from code_mule.scheduler import TaskScheduler
from code_mule.state import JsonProjectStateStore, serialize_project_state
from code_mule.worker import CodexWorkerSession, CodexTurnFailureKind
from code_mule.worker.contracts import turn_failure_details_from_metadata

from state import make_project_state
from worker.test_protocol import make_client, standard_handler
from worker.test_failure_diagnostics import failure_message, notification, SECRET
from test_worker_input_continuation_e2e import git


NOW = datetime(2026, 9, 5, tzinfo=UTC)


class TurnFailurePersistenceE2E(unittest.TestCase):
    def test_all_failure_branches_persist_safe_details_and_release_without_retry(self):
        for kind in CodexTurnFailureKind:
            with self.subTest(kind=kind), TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = root / "workspace"
                workspace.mkdir()
                git(workspace, "init", "-q")
                git(workspace, "config", "user.name", "Code Mule Test")
                git(workspace, "config", "user.email", "code-mule@example.invalid")
                (workspace / "README.md").write_text("baseline\n")
                git(workspace, "add", "--", "README.md")
                git(workspace, "commit", "-q", "-m", "initial")
                baseline = git(workspace, "rev-parse", "HEAD")
                state_file = root / "state.json"
                store = JsonProjectStateStore(state_file)
                source = make_project_state()
                task = replace(source.tasks[0], status=TaskStatus.PENDING, execution_attempts=0, dependencies=())
                store.save(replace(
                    source,
                    project=replace(source.project, status=ProjectStatus.RUNNING, current_task_id=None, workspace=str(workspace)),
                    tasks=(task,), execution_reports=(), decisions=(), events=(),
                ))
                calls = []

                def handler(process, message):
                    standard_handler(process, message)
                    if message["method"] == "turn/start":
                        calls.append(message["method"])
                        (workspace / "partial.py").write_text("# partial work preserved\n")
                        process.stdout.emit(notification("turn/started", turn={"id": "turn-1"}))
                        process.stdout.emit(notification("item/started", item={"type": "commandExecution"}))
                        process.stdout.emit(failure_message(kind))

                client, holder = make_client(handler, timeout=10)
                sessions = []

                def session_factory():
                    session = CodexWorkerSession(client._config, client_factory=lambda config: client)
                    sessions.append(session)
                    return session

                class Supervisor:
                    def review(self, request):
                        raise AssertionError("failed Worker must not reach Supervisor")

                cycle = TaskCycleService(
                    worker_session_factory=session_factory, supervisor=Supervisor(), store=store,
                    clock=lambda: NOW, report_id_factory=lambda: "report",
                    decision_id_factory=lambda: "decision", event_id_factory=lambda: "cycle-event",
                    config=TaskCycleConfig(2), git_delivery=GitDeliveryService(workspace, clock=lambda: NOW),
                )
                execution = ProjectExecutionService(
                    store=store, scheduler=TaskScheduler(), task_cycle_factory=lambda: cycle,
                    prompt_builder=TaskPromptBuilder(), clock=lambda: NOW,
                    event_id_factory=lambda: "execution-event", config=ProjectExecutionConfig(10),
                )
                from contextlib import nullcontext
                composition = ProductionCliComposition(
                    state_file, environment={}, stdout=StringIO(), stderr=StringIO(),
                    runtime_factory=lambda state: RuntimeComposition(
                        supervisor=object(), worker_service=object(), planning=object(),
                        execution=execution, change_execution=object(), renderer=nullcontext(),
                    ),
                )
                outcome = composition.run(None)
                self.assertNotEqual(outcome.exit_code, 0)
                state = store.load()
                action = state.human_actions[-1]
                self.assertIs(state.project.status, ProjectStatus.HUMAN_REQUIRED)
                self.assertIs(state.tasks[0].status, TaskStatus.IN_PROGRESS)
                self.assertIs(action.category, HumanActionCategory.RECOVERY_UNCERTAIN)
                self.assertIs(action.status, HumanActionStatus.PENDING)
                self.assertIs(state.execution_leases[-1].status, ExecutionLeaseStatus.RELEASED)
                self.assertEqual(state.execution_reports, ())
                self.assertEqual(state.decisions, ())
                self.assertEqual(state.git_commit_results, ())
                self.assertEqual(calls, ["turn/start"])
                self.assertEqual(len(sessions), 1)
                self.assertTrue(sessions[0].closed)
                self.assertTrue(holder["process"].terminated)
                self.assertEqual(client._reader_threads, [])
                self.assertEqual(git(workspace, "rev-parse", "HEAD"), baseline)
                self.assertEqual(git(workspace, "status", "--short"), "?? partial.py")
                event = next(e for e in state.events if e.event_type == "task.execution_failed")
                details = turn_failure_details_from_metadata(event.metadata)
                self.assertIs(details.kind, kind)
                self.assertEqual(details.activity_count, 2)
                self.assertEqual(details.error_code, "internal_error")
                self.assertNotIn(SECRET, repr(serialize_project_state(state)))
                before = state_file.read_bytes()
                verbose = "\n".join(composition.inspect(verbose=True).output)
                self.assertIn("Failure kind   " + kind.value, verbose)
                self.assertIn("Activity count 2", verbose)
                self.assertNotIn(SECRET, verbose)
                self.assertNotIn("WORKER FAILURE", "\n".join(composition.inspect().output))
                self.assertEqual(state_file.read_bytes(), before)
