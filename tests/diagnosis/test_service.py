from dataclasses import replace
from datetime import timedelta
import json
import unittest

from code_mule.diagnosis import (
    DiagnosisBlockerCategory,
    DiagnosisNextAction,
    DiagnosisRecoverability,
    ProjectDiagnosisService,
    DiagnosisStage,
)
from code_mule.domain import (
    HumanActionCategory,
    HumanActionStatus,
    ProjectEvent,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import HumanAction, WorkerInputDetails
from code_mule.human import request_human_action
from code_mule.recovery import ExecutionPhase
from code_mule.state.serialization import serialize_project_state
from tests.state import UPDATED, make_project_state


class ProjectDiagnosisServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ProjectDiagnosisService()

    @staticmethod
    def human_state(category, *, worker_input=None):
        state = make_project_state()
        action = HumanAction(
            "action-1",
            state.project.id,
            state.tasks[0].id,
            category,
            "Safe summary",
            "Inspect and decide",
            "Operation remains stopped",
            HumanActionStatus.PENDING,
            UPDATED,
            worker_input=worker_input,
        )
        return replace(
            state,
            project=replace(state.project, status=ProjectStatus.HUMAN_REQUIRED),
            human_actions=(action,),
        )

    def test_running_reports_plan_progress_and_current_task(self):
        state = make_project_state()
        diagnosis = self.service.diagnose(state)
        self.assertEqual(diagnosis.active_plan_version, 1)
        self.assertEqual((diagnosis.completed_tasks, diagnosis.total_tasks), (0, 1))
        self.assertEqual(diagnosis.current_task_id, "task-1")
        self.assertIs(diagnosis.blocker_category, DiagnosisBlockerCategory.NONE)
        self.assertFalse(diagnosis.boss_action_required)

    def test_latest_completed_task_is_visible_at_safe_boundary(self):
        state = make_project_state()
        task = replace(
            state.tasks[0],
            status=TaskStatus.COMPLETED,
            updated_at=UPDATED + timedelta(seconds=1),
        )
        state = replace(
            state,
            project=replace(state.project, current_task_id=None),
            tasks=(task,),
        )
        diagnosis = self.service.diagnose(state)
        self.assertEqual(diagnosis.latest_completed_task_id, "task-1")
        self.assertEqual(diagnosis.latest_completed_task_title, "Serialize state")

    def test_worker_input_exposes_only_typed_question(self):
        details = WorkerInputDetails(
            "item/tool/requestUserInput", "request-1", "Choose storage", ("local",), 1
        )
        diagnosis = self.service.diagnose(
            self.human_state(HumanActionCategory.WORKER_INPUT, worker_input=details)
        )
        self.assertIs(diagnosis.blocker_category, DiagnosisBlockerCategory.WORKER_INPUT)
        self.assertEqual(diagnosis.worker_input_question, "Choose storage")
        self.assertIs(diagnosis.recoverability, DiagnosisRecoverability.RECOVERABLE)
        self.assertIs(diagnosis.recommended_next_action, DiagnosisNextAction.INSPECT)

    def test_human_action_categories_are_deterministic(self):
        cases = {
            HumanActionCategory.WORKER_APPROVAL: DiagnosisBlockerCategory.WORKER_APPROVAL,
            HumanActionCategory.EXTERNAL_SIDE_EFFECT: DiagnosisBlockerCategory.EXTERNAL_SIDE_EFFECT,
            HumanActionCategory.WORKSPACE_BLOCK: DiagnosisBlockerCategory.WORKSPACE_BLOCK,
            HumanActionCategory.RECOVERY_UNCERTAIN: DiagnosisBlockerCategory.RECOVERY_UNCERTAIN,
            HumanActionCategory.ATTEMPT_LIMIT: DiagnosisBlockerCategory.ATTEMPT_LIMIT,
            HumanActionCategory.SUPERVISOR_FAILURE: DiagnosisBlockerCategory.SUPERVISOR_FAILURE,
            HumanActionCategory.DEPENDENCY_BLOCK: DiagnosisBlockerCategory.DEPENDENCY_BLOCK,
        }
        for category, expected in cases.items():
            with self.subTest(category=category):
                diagnosis = self.service.diagnose(self.human_state(category))
                self.assertIs(diagnosis.blocker_category, expected)
                self.assertTrue(diagnosis.boss_action_required)
                self.assertIs(diagnosis.recommended_next_action, DiagnosisNextAction.INSPECT)

    def test_planning_supervisor_failure_has_planning_stage_and_next_action(self):
        base = make_project_state()
        planning = replace(
            base,
            project=replace(
                base.project,
                status=ProjectStatus.PLANNING,
                active_plan_id=None,
                current_task_id=None,
                objective="Build it",
            ),
            plans=(),
            milestones=(),
            tasks=(),
        )
        identifiers = iter(("planning-failed", "action-requested"))
        state = request_human_action(
            planning,
            category=HumanActionCategory.SUPERVISOR_FAILURE,
            summary="Planning failed",
            requested_action="Inspect and resolve",
            risk="No Plan is trusted",
            task_id=None,
            operation_time=UPDATED,
            action_id="planning-action",
            event_id_factory=lambda: next(identifiers),
            source_event_types=("planning.failed",),
            source_metadata={"failure_category": "provider_authentication"},
            phase=ExecutionPhase.PLANNING,
        )

        diagnosis = self.service.diagnose(state)

        self.assertIs(diagnosis.blocker_stage, DiagnosisStage.PLANNING)
        self.assertIs(
            diagnosis.recommended_next_action, DiagnosisNextAction.INSPECT
        )
        self.assertNotEqual(diagnosis.recommended_next_action.value, "")

    def test_task_supervisor_failure_remains_task_review(self):
        diagnosis = self.service.diagnose(
            self.human_state(HumanActionCategory.SUPERVISOR_FAILURE)
        )
        self.assertIs(diagnosis.blocker_stage, DiagnosisStage.TASK_REVIEW)

    def test_worker_verification_uses_safe_typed_event_fields(self):
        state = self.human_state(HumanActionCategory.WORKER_VERIFICATION)
        event = ProjectEvent(
            "failure-1",
            state.project.id,
            "git.delivery_failed",
            "task-1",
            UPDATED,
            {
                "error_type": "WorkerVerificationError",
                "stage": "verification",
                "check_name": "Unit tests",
                "check_type": "test",
                "check_status": "fail",
                "check_required": "true",
            },
        )
        diagnosis = self.service.diagnose(replace(state, events=state.events + (event,)))
        self.assertIs(diagnosis.blocker_category, DiagnosisBlockerCategory.WORKER_VERIFICATION)
        self.assertEqual(diagnosis.verification.check_name, "Unit tests")
        self.assertEqual(diagnosis.verification.check_type.value, "test")
        self.assertEqual(diagnosis.verification.check_status.value, "fail")
        self.assertTrue(diagnosis.verification.required)

    def test_control_and_terminal_states_have_exact_actions(self):
        cases = (
            (ProjectStatus.PAUSED_BY_BOSS, DiagnosisBlockerCategory.PAUSED, DiagnosisNextAction.RESUME),
            (ProjectStatus.CHANGE_REQUESTED, DiagnosisBlockerCategory.CHANGE_REQUESTED, DiagnosisNextAction.APPLY_CHANGE),
            (
                ProjectStatus.DONE,
                DiagnosisBlockerCategory.NONE,
                DiagnosisNextAction.CHANGE,
            ),
            (ProjectStatus.CANCELLED, DiagnosisBlockerCategory.NONE, DiagnosisNextAction.NONE),
        )
        for status, blocker, action in cases:
            with self.subTest(status=status):
                state = make_project_state()
                task_status = (
                    TaskStatus.CANCELLED
                    if status is ProjectStatus.CANCELLED
                    else TaskStatus.COMPLETED
                )
                state = replace(
                    state,
                    project=replace(state.project, status=status, current_task_id=None),
                    tasks=(replace(state.tasks[0], status=task_status),),
                )
                diagnosis = self.service.diagnose(state)
                self.assertIs(diagnosis.blocker_category, blocker)
                self.assertIs(diagnosis.recommended_next_action, action)

    def test_inconsistent_state_fails_closed(self):
        state = make_project_state()
        state = replace(state, project=replace(state.project, active_plan_id="missing"))
        diagnosis = self.service.diagnose(state)
        self.assertIs(
            diagnosis.blocker_category, DiagnosisBlockerCategory.INCONSISTENT_STATE
        )
        self.assertIs(diagnosis.recoverability, DiagnosisRecoverability.UNCERTAIN)
        self.assertTrue(diagnosis.boss_action_required)

        orphaned = replace(
            make_project_state(),
            project=replace(make_project_state().project, current_task_id=None),
        )
        self.assertIs(
            self.service.diagnose(orphaned).blocker_category,
            DiagnosisBlockerCategory.INCONSISTENT_STATE,
        )

    def test_multiple_pending_actions_are_ambiguous(self):
        state = self.human_state(HumanActionCategory.WORKER_APPROVAL)
        state = replace(
            state,
            human_actions=(state.human_actions[0], replace(state.human_actions[0], id="action-2")),
        )
        self.assertIs(
            self.service.diagnose(state).blocker_category,
            DiagnosisBlockerCategory.INCONSISTENT_STATE,
        )

    def test_diagnosis_is_byte_for_byte_read_only(self):
        state = self.human_state(HumanActionCategory.RECOVERY_UNCERTAIN)
        before = json.dumps(serialize_project_state(state), sort_keys=True).encode()
        self.service.diagnose(state)
        after = json.dumps(serialize_project_state(state), sort_keys=True).encode()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
