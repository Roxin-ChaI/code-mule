from dataclasses import replace
from datetime import UTC, datetime
import unittest

from code_mule.diagnosis import (
    DiagnosisBlockerCategory,
    DiagnosisStage,
    ProjectDiagnosisService,
)
from code_mule.domain import (
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    PlanStatus,
    ProjectRevision,
    ProjectStatus,
    RevisionCheckStatus,
    TaskStatus,
)
from code_mule.human import HumanResolutionService, request_human_action
from code_mule.orchestrator import ChangeCommand, OrchestratorService
from code_mule.presentation import render_human_action
from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCheck,
    ProjectVerificationResult,
    ProjectVerificationStatus,
    final_review_human_judgment_evidence,
)
from code_mule.recovery import ExecutionPhase
from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    deserialize_project_state,
    serialize_project_state,
)

from state import CREATED, UPDATED, make_project_state


NOW = datetime(2026, 9, 11, 14, 4, 45, tzinfo=UTC)


class MemoryStore:
    def __init__(self, state):
        self.state = state

    def load(self):
        return self.state

    def save(self, state):
        self.state = state


def final_review_gate(*, legacy=False):
    source = make_project_state()
    plan = replace(source.plans[0], id="plan-2", version=2, status=PlanStatus.ACTIVE)
    milestone = replace(source.milestones[0], plan_id=plan.id)
    task = replace(source.tasks[0], status=TaskStatus.COMPLETED)
    check = ProjectVerificationCheck(
        "Git clean",
        ProjectVerificationCategory.GIT_CLEAN,
        ("git", "status", "--short"),
        ProjectVerificationStatus.PASS,
        0,
        "Repository is clean at the candidate HEAD.",
        True,
    )
    result = ProjectVerificationResult(
        "verification-2",
        source.project.id,
        plan.id,
        "a" * 40,
        "a" * 40,
        (check,),
        CREATED,
        UPDATED,
        FinalReviewDecision.HUMAN_REQUIRED,
        "Persistence conflicts with the Boss objective and needs explicit judgment.",
    )
    revision = ProjectRevision(
        2,
        CREATED,
        plan_id=plan.id,
        plan_version=2,
        base_revision=1,
        verification_status=RevisionCheckStatus.PASS,
        final_review_status=RevisionCheckStatus.UNKNOWN,
        verification_result_id=result.id,
    )
    prepared = replace(
        source,
        project=replace(
            source.project,
            status=ProjectStatus.RUNNING,
            active_plan_id=plan.id,
            current_task_id=None,
        ),
        plans=(plan,),
        milestones=(milestone,),
        tasks=(task,),
        change_requests=(),
        project_verification_results=(result,),
        revisions=(revision,),
        human_actions=(),
    )
    ids = iter(("requested", "source"))
    return request_human_action(
        prepared,
        category=(
            HumanActionCategory.SUPERVISOR_FAILURE
            if legacy
            else HumanActionCategory.FINAL_REVIEW_DECISION
        ),
        summary="Final Supervisor review requires human judgment",
        requested_action="Inspect the finding and request a correction",
        risk="Completion is not approved",
        task_id=None,
        operation_time=NOW,
        action_id="action-final",
        event_id_factory=lambda: next(ids),
        source_event_types=(
            "project.final_review_failed"
            if legacy
            else "project.final_review_human_judgment",
        ),
        source_metadata={
            "result_id": result.id,
            "issue_1": "Persistence contradicts the Boss objective.",
        },
        phase=ExecutionPhase.FINALIZATION,
    )


class FinalReviewHumanJudgmentTests(unittest.TestCase):
    def test_typed_and_legacy_gates_expose_bounded_persisted_finding(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                state = final_review_gate(legacy=legacy)
                action = state.human_actions[-1]
                evidence = final_review_human_judgment_evidence(state, action)
                self.assertIsNotNone(evidence)
                self.assertEqual(evidence.issues, ("Persistence contradicts the Boss objective.",))
                output = "\n".join(render_human_action(action, state=state))
                self.assertIn("Category    Final review decision", output)
                self.assertIn("Revision    2", output)
                self.assertIn("Plan        v2", output)
                self.assertIn("Verification Completed", output)
                self.assertIn("Persistence contradicts", output)
                self.assertIn("Accept as-is No", output)
                self.assertNotIn("raw response", output)

    def test_schema_v14_round_trip_preserves_safe_review_evidence(self):
        state = final_review_gate()
        payload = serialize_project_state(state)
        restored = deserialize_project_state(payload)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 16)
        self.assertEqual(serialize_project_state(restored), payload)
        evidence = final_review_human_judgment_evidence(
            restored, restored.human_actions[-1]
        )
        self.assertEqual(
            evidence.issues,
            ("Persistence contradicts the Boss objective.",),
        )

    def test_diagnosis_identifies_final_review_instead_of_supervisor_failure(self):
        diagnosis = ProjectDiagnosisService().diagnose(final_review_gate(legacy=True))
        self.assertIs(
            diagnosis.blocker_category,
            DiagnosisBlockerCategory.FINAL_REVIEW_DECISION,
        )
        self.assertIs(diagnosis.blocker_stage, DiagnosisStage.FINAL_REVIEW)
        self.assertEqual(diagnosis.revision_number, 2)
        self.assertEqual(diagnosis.active_plan_version, 2)
        self.assertEqual(diagnosis.project_verification_status, "completed")
        self.assertEqual(diagnosis.final_review_outcome, "human_judgment_required")

    def test_acknowledge_pauses_and_never_fabricates_final_review_pass(self):
        store = MemoryStore(final_review_gate())
        updated = HumanResolutionService(
            store,
            clock=lambda: NOW,
            event_id_factory=lambda: "resolution-event",
            resolution_id_factory=lambda: "resolution-ack",
        ).resolve("action-final", HumanResolutionStrategy.ACKNOWLEDGE)
        self.assertIs(updated.project.status, ProjectStatus.PAUSED_BY_BOSS)
        self.assertIs(updated.human_actions[-1].status, HumanActionStatus.RESOLVED)
        self.assertIs(
            updated.project_verification_results[-1].final_review_decision,
            FinalReviewDecision.HUMAN_REQUIRED,
        )
        self.assertIs(
            updated.revisions[-1].final_review_status,
            RevisionCheckStatus.UNKNOWN,
        )

    def test_direct_boss_change_closes_gate_and_preserves_revision_history(self):
        state = final_review_gate()
        before = state.revisions
        tasks_before = state.tasks
        store = MemoryStore(state)
        result = OrchestratorService(
            store,
            clock=lambda: NOW,
            event_id_factory=lambda: "event-change",
        ).change(ChangeCommand(state.project.id, "Restore non-persistent behavior", "boss", "CR-3"))
        self.assertIs(result.current_status, ProjectStatus.CHANGE_REQUESTED)
        self.assertEqual(store.state.revisions, before)
        self.assertEqual(store.state.tasks, tasks_before)
        self.assertIs(store.state.human_actions[-1].status, HumanActionStatus.RESOLVED)
        self.assertIs(
            store.state.human_resolutions[-1].strategy,
            HumanResolutionStrategy.REQUEST_CHANGE,
        )
        self.assertEqual(store.state.change_requests[-1].base_revision, 2)
        self.assertEqual(store.state.change_requests[-1].requested_revision, 2)

    def test_fail_project_is_explicit_and_deterministic(self):
        store = MemoryStore(final_review_gate())
        updated = HumanResolutionService(
            store,
            clock=lambda: NOW,
            event_id_factory=lambda: "resolution-event",
            resolution_id_factory=lambda: "resolution-fail",
        ).resolve("action-final", HumanResolutionStrategy.FAIL_PROJECT)
        self.assertIs(updated.project.status, ProjectStatus.FAILED)
        self.assertIs(updated.human_actions[-1].status, HumanActionStatus.RESOLVED)
        self.assertIs(
            updated.project_verification_results[-1].final_review_decision,
            FinalReviewDecision.HUMAN_REQUIRED,
        )


if __name__ == "__main__":
    unittest.main()
