import unittest
from dataclasses import replace

from code_mule.domain import ProjectStatus, SupervisorDecisionType, TaskStatus
from code_mule.orchestrator import OrchestratorService, StopCommand
from code_mule.runtime import ProjectExecutionStopReason

from .test_cycle import build_cycle, review
from .test_project import (
    FakeFinalizer,
    build_service,
    make_state,
    make_task,
)


class Ids:
    def __init__(self): self.value = 0
    def __call__(self):
        self.value += 1
        return f"stop-event-{self.value}"


class StopDuringReview:
    def __init__(self, store):
        self.store = store
        self.calls = 0

    def review(self, request):
        self.calls += 1
        OrchestratorService(
            self.store,
            clock=lambda: request.execution_report.created_at,
            event_id_factory=Ids(),
        ).stop(StopCommand(request.project_state.project.id))
        return review(SupervisorDecisionType.REWORK, "rework prompt")


class CancellationRuntimeTests(unittest.TestCase):
    def test_active_task_completes_then_no_next_task_is_dispatched(self):
        first = make_task("task-1")
        second = make_task("task-2", dependencies=("task-1",))
        state = make_state((first, second))
        finalizer = FakeFinalizer(None)
        service, store, cycles = build_service(
            state,
            cycle_options={
                "boundary_statuses": {"task-1": ProjectStatus.CANCEL_REQUESTED}
            },
        )
        finalizer = FakeFinalizer(store)
        service._finalizer = finalizer

        outcome = service.run()

        self.assertIs(outcome.stop_reason, ProjectExecutionStopReason.CANCELLED)
        self.assertIs(store.current.project.status, ProjectStatus.CANCELLED)
        self.assertEqual(cycles.requests[0].task.id, "task-1")
        self.assertEqual(len(cycles.requests), 1)
        statuses = {task.id: task.status for task in store.current.tasks}
        self.assertIs(statuses["task-1"], TaskStatus.COMPLETED)
        self.assertIs(statuses["task-2"], TaskStatus.CANCELLED)
        self.assertEqual(finalizer.calls, 0)

    def test_cancel_requested_at_idle_boundary_finalizes_without_worker_or_review(self):
        task = make_task("task-1")
        state = make_state(
            (task,), project_status=ProjectStatus.CANCEL_REQUESTED
        )
        service, store, cycles = build_service(state)
        outcome = service.run()
        self.assertIs(outcome.stop_reason, ProjectExecutionStopReason.CANCELLED)
        self.assertEqual(cycles.requests, [])
        self.assertIs(store.current.tasks[0].status, TaskStatus.CANCELLED)

    def test_already_cancelled_never_runs_final_verification(self):
        task = make_task("task-1", status=TaskStatus.CANCELLED)
        state = make_state((task,), project_status=ProjectStatus.CANCELLED)
        service, store, cycles = build_service(state)
        finalizer = FakeFinalizer(store)
        service._finalizer = finalizer
        outcome = service.run()
        self.assertIs(outcome.stop_reason, ProjectExecutionStopReason.CANCELLED)
        self.assertEqual(cycles.requests, [])
        self.assertEqual(finalizer.calls, 0)

    def test_stop_during_review_prevents_rework_and_closes_at_safe_point(self):
        service, request, store, session, _, _ = build_cycle()
        supervisor = StopDuringReview(store)
        service._supervisor = supervisor

        outcome = service.execute(request)

        self.assertTrue(outcome.cancelled)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(supervisor.calls, 1)
        self.assertIs(store.current.project.status, ProjectStatus.CANCEL_REQUESTED)
        self.assertIsNone(store.current.project.current_task_id)
        self.assertIs(store.current.tasks[0].status, TaskStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
