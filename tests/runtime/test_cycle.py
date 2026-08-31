import unittest
from dataclasses import replace
from datetime import UTC, datetime

from code_mule.domain.enums import (
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import ExecutionReport
from code_mule.progress import ProgressEventType, RecordingProgressSink
from code_mule.runtime import (
    InvalidTaskCycleState,
    TaskCycleConfig,
    TaskCycleRequest,
    TaskCycleService,
)
from code_mule.supervisor.contracts import ReviewResult
from code_mule.worker import CodexApprovalRequired, InvalidWorkerReport

from tests.state import make_project_state


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def cycle_state():
    source = make_project_state()
    task = replace(
        source.tasks[0],
        status=TaskStatus.IN_PROGRESS,
        execution_attempts=0,
    )
    return replace(
        source,
        project=replace(
            source.project,
            status=ProjectStatus.RUNNING,
            current_task_id=task.id,
        ),
        tasks=(task,),
        decisions=(),
        execution_reports=(),
    )


class FakeStore:
    def __init__(self, state, *, fail_on_save=None):
        self.current = state
        self.saved = []
        self.fail_on_save = fail_on_save

    def load(self):
        return self.current

    def save(self, state):
        save_number = len(self.saved) + 1
        if save_number == self.fail_on_save:
            raise OSError("persistence failed")
        self.saved.append(state)
        self.current = state


class FakeWorkerSession:
    def __init__(self, outcomes=None, *, human_action_required=False):
        self.outcomes = list(outcomes or [])
        self.human_action_required = human_action_required
        self.started = 0
        self.closed = 0
        self.requests = []
        self.thread_marker = object()

    def start(self):
        self.started += 1

    def execute(self, request, *, report_id, created_at):
        self.requests.append((request, report_id, created_at, self.thread_marker))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            status = outcome
        else:
            status = "completed"
        return ExecutionReport(
            id=report_id,
            task_id=request.task.id,
            attempt=request.task.execution_attempts + 1,
            status=status,
            files_changed=("calculator.py",),
            tests=("python -m unittest: pass",),
            static_checks=("compileall: pass",),
            git_state="dirty",
            issues=(),
            human_action_required=self.human_action_required,
            summary=f"attempt {request.task.execution_attempts + 1}",
            created_at=created_at,
        )

    def close(self):
        self.closed += 1


class FakeSupervisor:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class IdFactory:
    def __init__(self, prefix):
        self.prefix = prefix
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"{self.prefix}-{self.count}"


def review(decision, prompt=None, rationale="deterministic review"):
    return ReviewResult(decision, rationale, prompt, ())


def build_cycle(
    *,
    store=None,
    session=None,
    supervisor=None,
    max_attempts=3,
    progress_sink=None,
):
    store = store or FakeStore(cycle_state())
    session = session or FakeWorkerSession()
    supervisor = supervisor or FakeSupervisor(
        [review(SupervisorDecisionType.CONTINUE)]
    )
    sessions = []

    def session_factory():
        sessions.append(session)
        return session

    service = TaskCycleService(
        worker_session_factory=session_factory,
        supervisor=supervisor,
        store=store,
        clock=lambda: NOW,
        report_id_factory=IdFactory("report"),
        decision_id_factory=IdFactory("decision"),
        event_id_factory=IdFactory("event"),
        config=TaskCycleConfig(max_attempts),
        progress_sink=progress_sink,
    )
    request_task = (
        store.current.tasks[0]
        if store.current.tasks
        else cycle_state().tasks[0]
    )
    request = TaskCycleRequest(request_task, "initial prompt")
    return service, request, store, session, supervisor, sessions


class TaskCycleContractTests(unittest.TestCase):
    def test_config_and_request_validation(self):
        self.assertEqual(TaskCycleConfig(1).max_attempts, 1)
        for value in (0, -1):
            with self.assertRaises(ValueError):
                TaskCycleConfig(value)
        with self.assertRaises(ValueError):
            TaskCycleRequest(cycle_state().tasks[0], "")

    def test_start_preconditions_fail_closed_without_dependencies(self):
        base = cycle_state()
        states = (
            replace(base, tasks=()),
            replace(base, project=replace(base.project, current_task_id=None)),
            replace(
                base,
                tasks=(replace(base.tasks[0], status=TaskStatus.PENDING),),
            ),
            replace(
                base,
                project=replace(base.project, status=ProjectStatus.PAUSED_BY_BOSS),
            ),
        )
        for state in states:
            with self.subTest(state=state):
                store = FakeStore(state)
                session = FakeWorkerSession()
                supervisor = FakeSupervisor([])
                service, request, _, _, _, sessions = build_cycle(
                    store=store, session=session, supervisor=supervisor
                )
                with self.assertRaises(InvalidTaskCycleState):
                    service.execute(request)
                self.assertEqual(sessions, [])
                self.assertEqual(store.saved, [])


class TaskCycleFlowTests(unittest.TestCase):
    def test_rework_progress_exposes_attempts_and_supervisor_decisions(self):
        progress = RecordingProgressSink()
        supervisor = FakeSupervisor(
            [
                review(SupervisorDecisionType.REWORK, "repair addition"),
                review(SupervisorDecisionType.CONTINUE),
            ]
        )
        service, request, _, _, _, _ = build_cycle(
            supervisor=supervisor,
            progress_sink=progress,
        )
        service.execute(request)
        relevant = tuple(
            event
            for event in progress.events
            if event.type
            in {
                ProgressEventType.TASK_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
                ProgressEventType.TASK_REWORK,
                ProgressEventType.TASK_COMPLETED,
            }
        )
        self.assertEqual(
            tuple(event.type for event in relevant),
            (
                ProgressEventType.TASK_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
                ProgressEventType.TASK_REWORK,
                ProgressEventType.TASK_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
                ProgressEventType.TASK_COMPLETED,
            ),
        )
        self.assertEqual(
            tuple(
                event.attempt
                for event in relevant
                if event.type is ProgressEventType.TASK_STARTED
            ),
            (1, 2),
        )
        decisions = tuple(
            event.metadata["decision"]
            for event in relevant
            if event.type is ProgressEventType.SUPERVISOR_REVIEW_COMPLETED
        )
        self.assertEqual(decisions, ("rework", "continue"))

    def test_continue_runs_one_turn_persists_and_completes_only_current_task(self):
        original = cycle_state()
        store = FakeStore(original)
        service, request, store, session, supervisor, sessions = build_cycle(store=store)

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.CONTINUE)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(len(outcome.execution_reports), 1)
        self.assertEqual(len(outcome.decisions), 1)
        self.assertFalse(outcome.human_action_required)
        self.assertEqual(session.started, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(sessions, [session])
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(len(supervisor.requests), 1)
        reviewed_state = supervisor.requests[0].project_state
        self.assertEqual(reviewed_state.execution_reports[-1].id, "report-1")
        self.assertEqual(reviewed_state.tasks[0].execution_attempts, 1)
        self.assertEqual(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(store.current.tasks[0].execution_attempts, 1)
        self.assertIsNone(store.current.project.current_task_id)
        self.assertIs(store.current.project.status, ProjectStatus.RUNNING)
        self.assertEqual(outcome.execution_reports[0].created_at, NOW)
        self.assertEqual(outcome.decisions[0].created_at, NOW)
        self.assertEqual(outcome.execution_reports[0].id, "report-1")
        self.assertEqual(outcome.decisions[0].id, "decision-1")
        new_events = store.current.events[len(original.events) :]
        self.assertEqual(
            tuple(event.id for event in new_events),
            ("event-1", "event-2", "event-3", "event-4"),
        )
        self.assertEqual(
            tuple(event.event_type for event in new_events),
            (
                "task.execution_started",
                "task.execution_completed",
                "supervisor.review_completed",
                "task.completed",
            ),
        )
        self.assertNotIn("initial prompt", repr(new_events))
        self.assertEqual(original.tasks[0].execution_attempts, 0)
        self.assertEqual(original.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(original.execution_reports, ())
        self.assertEqual(original.decisions, ())

    def test_rework_reuses_same_session_and_second_prompt_then_continues(self):
        supervisor = FakeSupervisor(
            [
                review(SupervisorDecisionType.REWORK, "repair addition"),
                review(SupervisorDecisionType.CONTINUE),
            ]
        )
        service, request, store, session, supervisor, sessions = build_cycle(
            supervisor=supervisor
        )

        outcome = service.execute(request)

        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(len(outcome.execution_reports), 2)
        self.assertEqual(len(outcome.decisions), 2)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(session.started, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(
            [item[0].prompt for item in session.requests],
            ["initial prompt", "repair addition"],
        )
        self.assertIs(session.requests[0][3], session.requests[1][3])
        self.assertEqual(
            [item.attempt for item in outcome.execution_reports], [1, 2]
        )
        self.assertEqual(store.current.tasks[0].execution_attempts, 2)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertIn("task.rework_requested", event_types)

    def test_rework_at_limit_returns_human_required_without_fourth_turn(self):
        supervisor = FakeSupervisor(
            [
                review(SupervisorDecisionType.REWORK, "attempt 2"),
                review(SupervisorDecisionType.REWORK, "attempt 3"),
                review(SupervisorDecisionType.REWORK, "boss must inspect"),
            ]
        )
        service, request, store, session, _, _ = build_cycle(
            supervisor=supervisor, max_attempts=3
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertTrue(outcome.human_action_required)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(outcome.final_prompt, "boss must inspect")
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(store.current.tasks[0].execution_attempts, 3)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertIn("task.cycle_limit_reached", event_types)
        self.assertIn("task.human_required", event_types)

    def test_supervisor_human_required_stops_worker(self):
        supervisor = FakeSupervisor(
            [review(SupervisorDecisionType.HUMAN_REQUIRED)]
        )
        service, request, store, session, _, _ = build_cycle(supervisor=supervisor)

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(outcome.attempts, 1)
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_done_completes_task_but_does_not_complete_project(self):
        supervisor = FakeSupervisor([review(SupervisorDecisionType.DONE)])
        service, request, store, _, _, _ = build_cycle(supervisor=supervisor)
        outcome = service.execute(request)
        self.assertIs(outcome.final_decision, SupervisorDecisionType.DONE)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertIs(store.current.project.status, ProjectStatus.RUNNING)
        self.assertIsNone(store.current.project.current_task_id)


class TaskCycleFailureTests(unittest.TestCase):
    def test_structured_worker_human_gate_is_persisted_without_supervisor(self):
        session = FakeWorkerSession(human_action_required=True)
        supervisor = FakeSupervisor([])
        service, request, store, _, supervisor, _ = build_cycle(
            session=session, supervisor=supervisor
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(len(store.current.execution_reports), 1)
        self.assertEqual(supervisor.requests, [])
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_worker_approval_returns_human_required_without_supervisor(self):
        session = FakeWorkerSession([CodexApprovalRequired("approval")])
        supervisor = FakeSupervisor([])
        progress = RecordingProgressSink()
        service, request, store, session, supervisor, _ = build_cycle(
            session=session,
            supervisor=supervisor,
            progress_sink=progress,
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(outcome.attempts, 0)
        self.assertEqual(supervisor.requests, [])
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertIn("task.execution_failed", event_types)
        self.assertIn("task.human_required", event_types)
        progress_types = tuple(event.type for event in progress.events)
        self.assertIn(ProgressEventType.WORKER_FAILED, progress_types)
        self.assertIn(ProgressEventType.TASK_HUMAN_REQUIRED, progress_types)
        self.assertIn(ProgressEventType.HUMAN_GATE, progress_types)

    def test_malformed_worker_report_returns_human_without_prose_fallback(self):
        session = FakeWorkerSession([InvalidWorkerReport("malformed JSON")])
        supervisor = FakeSupervisor([])
        service, request, store, session, supervisor, _ = build_cycle(
            session=session, supervisor=supervisor
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(outcome.execution_reports, ())
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(store.current.execution_reports, ())

    def test_report_save_failure_stops_before_supervisor_without_reexecution(self):
        store = FakeStore(cycle_state(), fail_on_save=2)
        supervisor = FakeSupervisor([review(SupervisorDecisionType.CONTINUE)])
        service, request, store, session, supervisor, _ = build_cycle(
            store=store, supervisor=supervisor
        )

        with self.assertRaisesRegex(OSError, "persistence failed"):
            service.execute(request)

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(store.current.execution_reports, ())
        self.assertEqual(session.closed, 1)

    def test_supervisor_failure_keeps_report_persisted_and_does_not_retry(self):
        supervisor = FakeSupervisor([RuntimeError("provider failed")])
        progress = RecordingProgressSink()
        service, request, store, session, _, _ = build_cycle(
            supervisor=supervisor,
            progress_sink=progress,
        )

        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            service.execute(request)

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(len(store.current.execution_reports), 1)
        self.assertEqual(store.current.tasks[0].execution_attempts, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(
            tuple(
                event.type
                for event in progress.events
                if event.type
                in {
                    ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                    ProgressEventType.SUPERVISOR_FAILED,
                }
            ),
            (
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_FAILED,
            ),
        )


if __name__ == "__main__":
    unittest.main()
