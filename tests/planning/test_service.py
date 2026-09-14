from dataclasses import replace
from datetime import timedelta
import unittest

from code_mule.domain.enums import PlanStatus, ProjectStatus
from code_mule.domain.models import Plan
from code_mule.diagnosis import ProjectDiagnosisService
from code_mule.planning import (
    InvalidPlanProposal,
    PlanningValidationCode,
    ProjectPlanningRequest,
    ProjectPlanningService,
    ProjectPlanningStateError,
    SupervisorPlanningError,
)
from code_mule.progress import ProgressEventType, RecordingProgressSink
from code_mule.presentation import render_human_action
from code_mule.recovery import ExecutionPhase, ExecutionStopReason
from code_mule.supervisor import (
    SupervisorCallFailure,
    SupervisorFailureCategory,
    SupervisorOperation,
)

from planning.test_validation import NOW, empty_state, valid_proposal


class FakeStore:
    def __init__(self, state, *, fail_on_save: int | None = None):
        self.state = state
        self.fail_on_save = fail_on_save
        self.save_calls = 0
        self.saved = []

    def load(self):
        return self.state

    def save(self, state):
        self.save_calls += 1
        if self.save_calls == self.fail_on_save:
            raise OSError("save failed")
        self.state = state
        self.saved.append(state)


class FakeSupervisor:
    def __init__(self, proposal=None, error: Exception | None = None):
        self.proposal = proposal or valid_proposal()
        self.error = error
        self.requests = []

    def plan(self, request):
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
        return f"planning-event-{self.value}"


def make_service(store, supervisor, progress=None, plan_id="plan-generated"):
    return ProjectPlanningService(
        store=store,
        supervisor=supervisor,
        clock=TickingClock(),
        plan_id_factory=lambda: plan_id,
        event_id_factory=IdFactory(),
        progress_sink=progress,
    )


class ProjectPlanningServiceTests(unittest.TestCase):
    def test_initial_planning_persists_boundaries_and_returns_ready_outcome(self):
        store = FakeStore(empty_state())
        supervisor = FakeSupervisor()
        progress = RecordingProgressSink()
        service = make_service(store, supervisor, progress)

        outcome = service.plan(
            ProjectPlanningRequest("project-1", "Create a tiny calculator library")
        )

        self.assertEqual(store.save_calls, 2)
        self.assertIs(store.saved[0].project.status, ProjectStatus.PLANNING)
        self.assertEqual(store.saved[0].events[-1].event_type, "planning.started")
        self.assertIs(store.saved[1].project.status, ProjectStatus.RUNNING)
        self.assertEqual(
            tuple(item.event_type for item in store.saved[1].events[-2:]),
            ("planning.completed", "plan.materialized"),
        )
        self.assertEqual(len(supervisor.requests), 1)
        self.assertIs(
            supervisor.requests[0].project_state.project.status,
            ProjectStatus.PLANNING,
        )
        self.assertEqual(outcome.plan_id, "plan-generated")
        self.assertEqual(outcome.plan_version, 1)
        self.assertEqual(outcome.task_ids, ("T1", "T2"))
        self.assertTrue(outcome.ready_for_execution)
        self.assertEqual(
            tuple(event.type for event in progress.events),
            (
                ProgressEventType.PLANNING_STARTED,
                ProgressEventType.SUPERVISOR_PLAN_STARTED,
                ProgressEventType.SUPERVISOR_PLAN_COMPLETED,
                ProgressEventType.PLANNING_MATERIALIZING,
                ProgressEventType.PLANNING_COMPLETED,
            ),
        )
        self.assertEqual(progress.events[-1].metadata["total_tasks"], "2")

    def test_initial_state_guards_do_not_save_or_call_supervisor(self):
        states = (
            empty_state(status=ProjectStatus.RUNNING),
            empty_state(active_plan_id="existing"),
            empty_state(current_task_id="T1"),
            empty_state(
                plans=(
                    Plan(
                        "unbound-active",
                        "project-1",
                        1,
                        PlanStatus.ACTIVE,
                        (),
                        (),
                        NOW,
                    ),
                )
            ),
        )
        for state in states:
            with self.subTest(project=state.project):
                store = FakeStore(state)
                supervisor = FakeSupervisor()
                with self.assertRaises(ProjectPlanningStateError):
                    make_service(store, supervisor).plan(
                        ProjectPlanningRequest("project-1", "Objective")
                    )
                self.assertEqual(store.save_calls, 0)
                self.assertEqual(supervisor.requests, [])

        store = FakeStore(empty_state())
        supervisor = FakeSupervisor()
        with self.assertRaises(ProjectPlanningStateError):
            make_service(store, supervisor).plan(
                ProjectPlanningRequest("other-project", "Objective")
            )

    def test_supervisor_failure_is_persisted_once_without_retry(self):
        store = FakeStore(empty_state())
        supervisor = FakeSupervisor(error=RuntimeError("provider failed"))
        progress = RecordingProgressSink()

        with self.assertRaises(SupervisorPlanningError):
            make_service(store, supervisor, progress).plan(
                ProjectPlanningRequest("project-1", "Objective")
            )

        self.assertEqual(len(supervisor.requests), 1)
        self.assertEqual(store.save_calls, 2)
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(store.state.events[-1].event_type, "planning.failed")
        self.assertEqual(store.state.plans, ())
        self.assertEqual(store.state.execution_attempts, ())
        self.assertIs(
            store.state.latest_execution_stop.phase, ExecutionPhase.PLANNING
        )
        self.assertIs(
            store.state.latest_execution_stop.reason,
            ExecutionStopReason.SUPERVISOR_FAILED,
        )
        self.assertFalse(store.state.latest_execution_stop.worker_started)
        self.assertNotIn("provider failed", str(store.state.events[-1].metadata))
        self.assertIs(progress.events[-1].type, ProgressEventType.PLANNING_FAILED)

    def test_retry_exhaustion_metadata_reaches_human_action_audit(self):
        store = FakeStore(empty_state())
        failure = SupervisorCallFailure(
            operation=SupervisorOperation.PLAN,
            failure_category=(
                SupervisorFailureCategory.SCHEMA_CONTRACT_VIOLATION
            ),
            attempt_count=2,
            retryable=True,
            exhausted=True,
        )
        supervisor = FakeSupervisor(error=failure)

        with self.assertRaises(SupervisorPlanningError):
            make_service(store, supervisor).plan(
                ProjectPlanningRequest("project-1", "Objective")
            )

        metadata = store.state.events[-1].metadata
        self.assertEqual(metadata["operation"], "plan")
        self.assertEqual(metadata["attempt_count"], "2")
        self.assertEqual(
            metadata["failure_category"], "schema_contract_violation"
        )

    def test_invalid_proposal_is_rejected_without_repair_or_second_call(self):
        proposal = valid_proposal()
        invalid = replace(
            proposal,
            tasks=(
                replace(proposal.tasks[0], requirement_ids=("UNKNOWN",)),
                proposal.tasks[1],
            ),
        )
        store = FakeStore(empty_state())
        supervisor = FakeSupervisor(proposal=invalid)

        with self.assertRaises(InvalidPlanProposal):
            make_service(store, supervisor).plan(
                ProjectPlanningRequest("project-1", "Objective")
            )

        self.assertEqual(len(supervisor.requests), 1)
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(
            store.state.events[-1].event_type, "planning.proposal_rejected"
        )
        self.assertEqual(store.state.plans, ())

    def test_invalid_proposal_persists_safe_typed_diagnostics(self):
        secret = "sk-not-persisted"
        proposal = replace(
            valid_proposal(),
            tasks=(
                replace(
                    valid_proposal().tasks[0],
                    requirement_ids=(secret,),
                ),
                valid_proposal().tasks[1],
            ),
        )
        store = FakeStore(empty_state())

        with self.assertRaises(InvalidPlanProposal):
            make_service(store, FakeSupervisor(proposal=proposal)).plan(
                ProjectPlanningRequest("project-1", "Verify existing service")
            )

        event = store.state.events[-1]
        self.assertEqual(
            event.metadata["validation_code"],
            PlanningValidationCode.UNKNOWN_REQUIREMENT_REFERENCE.value,
        )
        self.assertEqual(
            event.metadata["field_path"], "tasks[0].requirement_ids[0]"
        )
        self.assertEqual(event.metadata["stage"], "proposal_validation")
        self.assertEqual(event.metadata["retryable"], "false")
        self.assertEqual(event.metadata["plan_created"], "false")
        self.assertEqual(event.metadata["worker_started"], "false")
        self.assertNotIn(secret, str(event.metadata))
        self.assertNotIn(secret, str(store.state.human_actions))

        diagnosis = ProjectDiagnosisService().diagnose(store.state)
        self.assertEqual(
            diagnosis.failure_code,
            PlanningValidationCode.UNKNOWN_REQUIREMENT_REFERENCE.value,
        )
        self.assertEqual(
            diagnosis.failure_field_path, "tasks[0].requirement_ids[0]"
        )
        self.assertIn("Requirement", diagnosis.blocker_summary)

        rendered = "\n".join(
            render_human_action(
                store.state.human_actions[-1], state=store.state, verbose=True
            )
        )
        self.assertIn("Validation   Unknown requirement reference", rendered)
        self.assertIn("Field        tasks[0].requirement_ids[0]", rendered)
        self.assertNotIn(secret, rendered)

    def test_start_save_failure_prevents_supervisor_call(self):
        store = FakeStore(empty_state(), fail_on_save=1)
        supervisor = FakeSupervisor()
        with self.assertRaises(OSError):
            make_service(store, supervisor).plan(
                ProjectPlanningRequest("project-1", "Objective")
            )
        self.assertEqual(supervisor.requests, [])
        self.assertIs(store.state.project.status, ProjectStatus.IDLE)

    def test_atomic_materialization_save_failure_never_persists_running(self):
        store = FakeStore(empty_state(), fail_on_save=2)
        supervisor = FakeSupervisor()
        with self.assertRaises(OSError):
            make_service(store, supervisor).plan(
                ProjectPlanningRequest("project-1", "Objective")
            )
        self.assertEqual(len(supervisor.requests), 1)
        self.assertEqual(store.save_calls, 2)
        self.assertIs(store.state.project.status, ProjectStatus.PLANNING)
        self.assertEqual(store.state.plans, ())
        self.assertEqual(store.state.tasks, ())


if __name__ == "__main__":
    unittest.main()
