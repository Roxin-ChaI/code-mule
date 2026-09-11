from dataclasses import replace
from datetime import timedelta
import unittest

from code_mule.diagnosis import DiagnosisStage, ProjectDiagnosisService
from code_mule.domain import (
    HumanAction,
    HumanActionCategory,
    HumanActionStatus,
    PlanStatus,
    ProjectEvent,
    ProjectRevision,
    ProjectStatus,
    RevisionStatus,
)
from code_mule.domain.models import ExecutionReport
from code_mule.execution import ExecutionLease, ExecutionLeaseStatus
from code_mule.git_delivery import GitBaseline
from code_mule.presentation import render_human_action, render_project_diagnosis
from code_mule.recovery.contracts import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    WorkerOwnershipStatus,
    WorkerStopCause,
    WorkerWorkspaceState,
)
from code_mule.recovery.uncertainty import worker_uncertainty_evidence
from code_mule.recovery.service import RecoveryClassifier
from code_mule.recovery import RecoveryMode
from code_mule.state.serialization import deserialize_project_state, serialize_project_state
from tests.state import CREATED, UPDATED, make_project_state


THREAD = "thread-plan-v3"
TURN = "turn-plan-v3-2"
HEAD = "a" * 40
PATHS = ("README.md", "scripts/verify.py")


def uncertain_state(*, exact_paths=False):
    source = make_project_state()
    plan = replace(source.plans[0], id="plan-v3", version=3, status=PlanStatus.ACTIVE)
    task = replace(
        source.tasks[0],
        id="TASK-007",
        title="Verify active acceptance criteria",
        milestone_id=source.milestones[0].id,
        execution_attempts=1,
    )
    action = HumanAction(
        "action-uncertain",
        source.project.id,
        task.id,
        HumanActionCategory.RECOVERY_UNCERTAIN,
        "Codex Worker stopped with uncertain execution ownership",
        "Inspect repository state before choosing an explicit resolution",
        "Retrying may duplicate an operation whose outcome is uncertain",
        HumanActionStatus.PENDING,
        UPDATED,
    )
    metadata = {
        "error_type": "CodexTurnFailed",
        "failure_kind": "error_notification",
        "thread_id": THREAD,
        "turn_id": TURN,
        "will_retry": "false",
        "activity_count": "21",
        "last_activity_age_seconds": "3.5",
        "turn_elapsed_seconds": "105.0",
    }
    if exact_paths:
        metadata.update(
            {
                "partial_path_count": "2",
                "partial_path_1": PATHS[0],
                "partial_path_2": PATHS[1],
            }
        )
    report = ExecutionReport(
        "report-attempt-1",
        task.id,
        1,
        "completed",
        PATHS,
        ("tests: pass",),
        ("compileall: pass",),
        "dirty",
        (),
        None,
        "trusted report",
        UPDATED - timedelta(minutes=3),
    )
    milestone = replace(
        source.milestones[0], plan_id=plan.id, task_ids=(task.id,)
    )
    return replace(
        source,
        project=replace(
            source.project,
            status=ProjectStatus.HUMAN_REQUIRED,
            active_plan_id=plan.id,
            current_task_id=task.id,
        ),
        plans=(replace(source.plans[0], status=PlanStatus.SUPERSEDED), plan),
        milestones=(milestone,),
        tasks=(task,),
        execution_reports=(report,),
        human_actions=(action,),
        revisions=(
            ProjectRevision(
                2,
                CREATED,
                RevisionStatus.IN_PROGRESS,
                plan.id,
                plan.version,
            ),
        ),
        git_baselines=(GitBaseline(task.id, "/repo", HEAD, ()),),
        execution_attempts=(
            ExecutionAttempt(
                task.id,
                1,
                ExecutionAttemptStatus.REVIEW_COMPLETED,
                CREATED,
                THREAD,
                "turn-plan-v3-1",
                HEAD,
                UPDATED - timedelta(minutes=2),
            ),
            ExecutionAttempt(
                task.id,
                2,
                ExecutionAttemptStatus.UNCERTAIN,
                UPDATED - timedelta(minutes=2),
                THREAD,
                None,
                HEAD,
                UPDATED,
                "codexturnfailed",
                True,
            ),
        ),
        execution_leases=(
            ExecutionLease(
                "lease-plan-v3",
                source.project.id,
                "owner-plan-v3",
                123,
                UPDATED - timedelta(minutes=14),
                UPDATED,
                ExecutionLeaseStatus.RELEASED,
                task.id,
                THREAD,
                2,
            ),
        ),
        events=source.events
        + (
            ProjectEvent(
                "failure-plan-v3",
                source.project.id,
                "task.execution_failed",
                task.id,
                UPDATED,
                metadata,
            ),
        ),
    )


class WorkerUncertaintyEvidenceTests(unittest.TestCase):
    def test_projects_exact_plan_v3_worker_lifecycle_without_claiming_retry(self):
        state = uncertain_state()
        evidence = worker_uncertainty_evidence(state, state.human_actions[0])

        self.assertEqual((evidence.revision_number, evidence.plan_version), (2, 3))
        self.assertEqual(evidence.attempt, 2)
        self.assertTrue(evidence.worker_started)
        self.assertEqual(evidence.last_trusted_stage, "Worker activity observed")
        self.assertFalse(evidence.trusted_terminal_result)
        self.assertFalse(evidence.report_persisted)
        self.assertIs(evidence.workspace_state, WorkerWorkspaceState.CHANGED)
        self.assertEqual(evidence.partial_paths, PATHS)
        self.assertFalse(evidence.partial_paths_complete)
        self.assertFalse(evidence.commit_created)
        self.assertIs(evidence.ownership_status, WorkerOwnershipStatus.RELEASED_MATCHED)
        self.assertIs(evidence.stop_cause, WorkerStopCause.ERROR_NOTIFICATION)
        self.assertFalse(evidence.retry_safe)

    def test_current_attempt_report_and_commit_are_not_inferred_from_prior_attempt(self):
        evidence = worker_uncertainty_evidence(
            uncertain_state(), uncertain_state().human_actions[0]
        )
        self.assertFalse(evidence.report_persisted)
        self.assertFalse(evidence.commit_created)

    def test_exact_future_partial_path_metadata_is_preferred(self):
        state = uncertain_state(exact_paths=True)
        evidence = worker_uncertainty_evidence(state, state.human_actions[0])
        self.assertEqual(evidence.partial_paths, PATHS)
        self.assertTrue(evidence.partial_paths_complete)

    def test_malformed_path_metadata_falls_back_without_echoing_extras(self):
        state = uncertain_state(exact_paths=True)
        event = state.events[-1]
        metadata = dict(event.metadata)
        metadata["partial_path_count"] = "999999"
        metadata["raw_payload"] = "secret prompt"
        state = replace(state, events=state.events[:-1] + (replace(event, metadata=metadata),))
        evidence = worker_uncertainty_evidence(state, state.human_actions[0])
        self.assertEqual(evidence.partial_paths, PATHS)
        self.assertFalse(evidence.partial_paths_complete)
        self.assertNotIn("secret", repr(evidence))

    def test_identity_mismatch_stays_uncertain(self):
        state = uncertain_state()
        lease = replace(state.execution_leases[0], codex_thread_id="other-thread")
        state = replace(state, execution_leases=(lease,))
        evidence = worker_uncertainty_evidence(state, state.human_actions[0])
        self.assertIs(evidence.ownership_status, WorkerOwnershipStatus.IDENTITY_MISMATCH)
        self.assertFalse(evidence.retry_safe)

    def test_released_plan_v2_lease_cannot_leak_into_plan_v3_identity(self):
        state = uncertain_state()
        old = replace(
            state.execution_leases[0],
            id="lease-plan-v2",
            current_task_id="TASK-006",
            codex_thread_id="thread-plan-v2",
            attempt=1,
            acquired_at=CREATED,
        )
        state = replace(state, execution_leases=(old,) + state.execution_leases)

        evidence = worker_uncertainty_evidence(state, state.human_actions[0])

        self.assertIs(evidence.ownership_status, WorkerOwnershipStatus.RELEASED_MATCHED)
        self.assertEqual(evidence.task_id, "TASK-007")
        self.assertEqual((evidence.revision_number, evidence.plan_version), (2, 3))

    def test_restart_with_uncertain_worker_is_blocked_without_dispatch_permission(self):
        state = uncertain_state(exact_paths=True)

        recovery = RecoveryClassifier().classify(state)

        self.assertIs(recovery.recovery_mode, RecoveryMode.BLOCKED)
        self.assertTrue(recovery.requires_boss_action)
        self.assertFalse(recovery.automatic_resume_allowed)
        self.assertFalse(recovery.fresh_worker_required)

    def test_inspect_exposes_bounded_lifecycle_and_acknowledge_semantics(self):
        state = uncertain_state()
        text = "\n".join(render_human_action(state.human_actions[0], state=state))
        for expected in (
            "RECOVERY UNCERTAIN",
            "Revision          2",
            "Plan              v3",
            "Attempt           2",
            "Worker started    Yes",
            "Terminal result   Missing",
            "Report persisted  No",
            "Workspace changed Changed",
            "Commit created    No",
            "Ownership status  Released matched",
            "Stop cause        Error notification",
            "Retry safe        No",
            "Human Gate stays pending",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("secret", text)

    def test_diagnosis_exposes_worker_stage_revision_plan_and_retry_safety(self):
        diagnosis = ProjectDiagnosisService().diagnose(uncertain_state())
        self.assertIs(diagnosis.blocker_stage, DiagnosisStage.WORKER_EXECUTION)
        self.assertEqual((diagnosis.revision_number, diagnosis.active_plan_version), (2, 3))
        self.assertFalse(diagnosis.worker_uncertainty.retry_safe)
        text = "\n".join(render_project_diagnosis(diagnosis))
        self.assertIn("Worker execution", text)
        self.assertIn("Stop cause        Error notification", text)
        self.assertIn("Retry safe        No", text)

    def test_projection_is_read_only_and_schema_v14_round_trips(self):
        state = uncertain_state(exact_paths=True)
        before = serialize_project_state(state)
        worker_uncertainty_evidence(state, state.human_actions[0])
        diagnosis = ProjectDiagnosisService().diagnose(state)
        render_project_diagnosis(diagnosis)
        self.assertEqual(serialize_project_state(state), before)
        self.assertEqual(
            serialize_project_state(deserialize_project_state(before)), before
        )


if __name__ == "__main__":
    unittest.main()
