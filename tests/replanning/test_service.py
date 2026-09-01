from dataclasses import replace
from datetime import timedelta
import unittest

from code_mule.domain.enums import ChangeRequestStatus, ProjectStatus
from code_mule.replanning import (
    ChangeReplanningRequest,
    ChangeReplanningService,
    InvalidReplanProposal,
    InvalidReplanningState,
    SupervisorReplanningError,
)
from code_mule.progress import ProgressEventType, RecordingProgressSink

from replanning.test_validation import NOW, state, valid_proposal


class FakeStore:
    def __init__(self, initial, *, fail_on_save=None):
        self.state = initial
        self.saved = []
        self.save_calls = 0
        self.fail_on_save = fail_on_save

    def load(self):
        return self.state

    def save(self, state):
        self.save_calls += 1
        if self.save_calls == self.fail_on_save:
            raise OSError("save failed")
        self.state = state
        self.saved.append(state)


class FakeSupervisor:
    def __init__(self, proposal=None, error=None):
        self.proposal = proposal or valid_proposal()
        self.error = error
        self.requests = []

    def analyze_change(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.proposal


class TickingClock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        value = NOW + timedelta(seconds=self.calls)
        self.calls += 1
        return value


class IdFactory:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"replan-event-{self.value}"


def service(store, supervisor, progress=None, plan_id="PLAN-2"):
    return ChangeReplanningService(
        store=store,
        supervisor=supervisor,
        clock=TickingClock(),
        plan_id_factory=lambda: plan_id,
        event_id_factory=IdFactory(),
        progress_sink=progress,
    )


class ChangeReplanningServiceTests(unittest.TestCase):
    def test_valid_change_persists_boundaries_and_returns_plan_v2(self):
        store = FakeStore(state())
        supervisor = FakeSupervisor()
        progress = RecordingProgressSink()

        outcome = service(store, supervisor, progress).replan(
            ChangeReplanningRequest("project-1", "CHANGE-1")
        )

        self.assertEqual(store.save_calls, 2)
        self.assertIs(store.saved[0].project.status, ProjectStatus.REPLANNING)
        self.assertIs(
            store.saved[0].change_requests[0].status,
            ChangeRequestStatus.ANALYZING,
        )
        self.assertIs(store.state.project.status, ProjectStatus.RUNNING)
        self.assertEqual(outcome.previous_plan_version, 1)
        self.assertEqual(outcome.plan_version, 2)
        self.assertTrue(outcome.ready_for_execution)
        self.assertEqual(len(supervisor.requests), 1)
        self.assertEqual(
            tuple(event.type for event in progress.events),
            (
                ProgressEventType.REPLANNING_STARTED,
                ProgressEventType.SUPERVISOR_IMPACT_STARTED,
                ProgressEventType.SUPERVISOR_IMPACT_COMPLETED,
                ProgressEventType.REPLANNING_MATERIALIZING,
                ProgressEventType.REPLANNING_COMPLETED,
            ),
        )
        self.assertEqual(
            tuple(item.event_type for item in store.state.events),
            (
                "replanning.started",
                "supervisor.impact_completed",
                "replanning.materializing",
                "replanning.completed",
            ),
        )

    def test_preconditions_fail_before_save_or_supervisor(self):
        invalid_states = (
            replace(state(), project=replace(state().project, status=ProjectStatus.RUNNING)),
            replace(state(), project=replace(state().project, current_task_id="T2")),
            replace(state(), project=replace(state().project, active_plan_id=None)),
        )
        for initial in invalid_states:
            with self.subTest(project=initial.project):
                store = FakeStore(initial)
                supervisor = FakeSupervisor()
                with self.assertRaises(InvalidReplanningState):
                    service(store, supervisor).replan(
                        ChangeReplanningRequest("project-1", "CHANGE-1")
                    )
                self.assertEqual(store.save_calls, 0)
                self.assertEqual(supervisor.requests, [])

    def test_supervisor_failure_is_persisted_without_retry(self):
        store = FakeStore(state())
        supervisor = FakeSupervisor(error=RuntimeError("provider failed"))
        with self.assertRaises(SupervisorReplanningError):
            service(store, supervisor).replan(
                ChangeReplanningRequest("project-1", "CHANGE-1")
            )
        self.assertEqual(len(supervisor.requests), 1)
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(store.state.events[-1].event_type, "replanning.failed")

    def test_invalid_proposal_is_rejected_without_repair_or_retry(self):
        invalid = replace(valid_proposal(), affected_task_ids=("unknown",))
        store = FakeStore(state())
        supervisor = FakeSupervisor(proposal=invalid)
        with self.assertRaises(InvalidReplanProposal):
            service(store, supervisor).replan(
                ChangeReplanningRequest("project-1", "CHANGE-1")
            )
        self.assertEqual(len(supervisor.requests), 1)
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(
            store.state.events[-1].event_type,
            "replanning.proposal_rejected",
        )

    def test_materialization_save_failure_leaves_replanning_and_no_running(self):
        store = FakeStore(state(), fail_on_save=2)
        supervisor = FakeSupervisor()
        with self.assertRaises(OSError):
            service(store, supervisor).replan(
                ChangeReplanningRequest("project-1", "CHANGE-1")
            )
        self.assertEqual(len(supervisor.requests), 1)
        self.assertIs(store.state.project.status, ProjectStatus.REPLANNING)
        self.assertEqual(len(store.state.plans), 1)


if __name__ == "__main__":
    unittest.main()
