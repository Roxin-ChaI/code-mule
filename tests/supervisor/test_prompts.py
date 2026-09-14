import unittest
from dataclasses import replace
from datetime import UTC, datetime

from code_mule.project_verification import (
    ProjectVerificationCategory,
    ProjectVerificationCheck,
    ProjectVerificationResult,
    ProjectVerificationStatus,
)
from code_mule.domain import ChangeRequestStatus, ProjectRevision
from code_mule.supervisor.contracts import (
    FinalReviewRequest,
    ImpactAnalysisRequest,
    PlanRequest,
    ProgressReportRequest,
    ReviewRequest,
)
from code_mule.supervisor.prompts import (
    SUPERVISOR_SYSTEM_POLICY,
    build_impact_analysis_prompt,
    build_plan_prompt,
    build_progress_report_prompt,
    build_review_prompt,
    build_final_review_prompt,
)

from state import make_project_state


class SupervisorPromptTests(unittest.TestCase):
    def test_final_review_prompt_contains_only_persisted_completion_evidence(self):
        state = make_project_state()
        now = datetime(2026, 9, 3, tzinfo=UTC)
        result = ProjectVerificationResult(
            "verification-1", state.project.id, state.project.active_plan_id,
            "a" * 40, "a" * 40,
            (
                ProjectVerificationCheck(
                    "Tests", ProjectVerificationCategory.TEST,
                    ("python", "-m", "unittest"),
                    ProjectVerificationStatus.PASS, 0,
                    "Verification command passed.", True,
                ),
            ),
            now, now,
        )
        system, prompt = build_final_review_prompt(FinalReviewRequest(state, result))
        self.assertIn("Current operation: final_review", system)
        self.assertIn("Operation: FINAL_REVIEW", prompt)
        self.assertIn("Verified HEAD", prompt)
        self.assertIn("test: Tests = pass", prompt)
        self.assertIn("return HUMAN_REQUIRED", prompt)
        self.assertIn("Do not return REWORK", prompt)
        self.assertIn("active Plan remains active", prompt)
        self.assertIn("Boss-relevant finding", prompt)
        self.assertNotIn("Generate a shell command", prompt)

    def test_final_review_includes_applied_boss_change_for_current_revision(self):
        state = make_project_state()
        change = replace(
            state.change_requests[0],
            status=ChangeRequestStatus.APPLIED,
            description="Persist with localStorage",
            created_by="boss",
            requested_revision=2,
            base_revision=1,
            base_plan_id=state.plans[0].id,
            base_plan_version=1,
        )
        plan = replace(
            state.plans[0],
            version=2,
            change_request_id=change.id,
            revision_number=2,
        )
        revision = ProjectRevision(
            2,
            state.project.created_at,
            plan_id=plan.id,
            plan_version=2,
            base_revision=1,
            change_request_id=change.id,
        )
        state = replace(
            state,
            plans=(plan,),
            change_requests=(change,),
            revisions=(revision,),
        )
        now = datetime(2026, 9, 3, tzinfo=UTC)
        result = ProjectVerificationResult(
            "verification-2", state.project.id, plan.id,
            "a" * 40, "a" * 40, (), now, now,
        )

        _, prompt = build_final_review_prompt(FinalReviewRequest(state, result))

        self.assertIn("Revision: 2", prompt)
        self.assertIn("Boss-authorized revision change", prompt)
        self.assertIn("description=\"Persist with localStorage\"", prompt)
        self.assertIn("created_by=\"boss\"", prompt)
        self.assertIn("explicit Boss authorization", prompt)

    def test_plan_prompt_contains_current_facts_and_context(self):
        state = make_project_state()
        system_prompt, user_prompt = build_plan_prompt(
            PlanRequest(state, "Implement Phase 4")
        )

        self.assertIn(
            "existing repository implementation may be preserved", user_prompt
        )
        self.assertIn("one-Milestone, one-Task Plan is valid", user_prompt)
        self.assertIn("Do not emit a DeliveryManifest", user_prompt)
        self.assertIn(
            "Code Mule materializes a verified DeliveryManifest", user_prompt
        )

        self.assertIn("You are the Supervisor for Code Mule.", system_prompt)
        self.assertIn("Current operation: plan.", system_prompt)
        self.assertIn('id="project-1"', user_prompt)
        self.assertIn('name="Code Mule"', user_prompt)
        self.assertIn("status=running", user_prompt)
        self.assertIn('current_task_id="task-1"', user_prompt)
        self.assertIn('id="req-2"', user_prompt)
        self.assertIn('id="plan-1"', user_prompt)
        self.assertIn("Quality Status:", user_prompt)
        self.assertIn("Operation: PLAN", user_prompt)
        self.assertIn('Objective: "Implement Phase 4"', user_prompt)
        self.assertIn("smallest deliverable requirements", user_prompt)
        self.assertIn("one Codex execution cycle", user_prompt)
        self.assertIn("Every task must reference", user_prompt)
        self.assertIn("do not add deployment or release", user_prompt)
        self.assertIn(
            'Existing Requirement IDs: ["req-2", "req-1"]', user_prompt
        )
        self.assertIn(
            "requirements_considered may contain only IDs", user_prompt
        )
        self.assertIn(
            "exactly the fields defined by the response schema", user_prompt
        )
        self.assertIn("Never add placeholder, helper", user_prompt)
        self.assertIn("return an empty array for that official field", user_prompt)
        self.assertNotIn("not_used", user_prompt)
        self.assertLess(user_prompt.index('id="req-2"'), user_prompt.index('id="req-1"'))
        self.assertNotIn("ProjectState(", user_prompt)
        self.assertNotIn("Requirement(", user_prompt)
        self.assertNotIn("Project(", user_prompt)

    def test_plan_prompt_requires_empty_considered_ids_when_none_exist(self):
        state = replace(make_project_state(), requirements=())
        _, user_prompt = build_plan_prompt(PlanRequest(state, "Create calculator"))

        self.assertIn("Existing Requirement IDs: []", user_prompt)
        self.assertIn(
            'exactly "requirements_considered": []', user_prompt
        )
        self.assertIn(
            'Correct: "requirements_considered": []', user_prompt
        )
        self.assertIn(
            'Incorrect: "requirements_considered": '
            '["No existing requirements exist"]',
            user_prompt,
        )
        self.assertIn(
            "Put new requirements in requirements, not in requirements_considered",
            user_prompt,
        )

    def test_operation_builders_include_explicit_current_context(self):
        state = make_project_state()
        cases = (
            (
                build_review_prompt(
                    ReviewRequest(state, state.tasks[0], state.execution_reports[0])
                ),
                "Operation: REVIEW",
                "Execution report under review",
            ),
            (
                build_impact_analysis_prompt(
                    ImpactAnalysisRequest(state, state.change_requests[0])
                ),
                "Operation: IMPACT_ANALYSIS",
                "Change request under analysis",
            ),
            (
                build_progress_report_prompt(
                    ProgressReportRequest(state, "What remains?")
                ),
                "Operation: PROGRESS_REPORT",
                'Question: "What remains?"',
            ),
        )
        for prompts, operation, context in cases:
            with self.subTest(operation=operation):
                system_prompt, user_prompt = prompts
                self.assertIn(operation, user_prompt)
                self.assertIn(context, user_prompt)
                self.assertIn(SUPERVISOR_SYSTEM_POLICY, system_prompt)

    def test_review_prompt_enforces_decision_prompt_combinations(self):
        state = make_project_state()
        _, prompt = build_review_prompt(
            ReviewRequest(state, state.tasks[0], state.execution_reports[0])
        )

        self.assertIn(
            "HUMAN_REQUIRED and DONE require next_task_prompt=null",
            prompt,
        )
        self.assertIn(
            "REWORK requires a non-empty next_task_prompt",
            prompt,
        )
        self.assertIn(
            "CONTINUE may use null or a string under its existing semantics",
            prompt,
        )
        self.assertIn("Do not use an empty string instead of null", prompt)
        self.assertIn(
            "never generate a Worker instruction for HUMAN_REQUIRED",
            prompt,
        )
        self.assertIn(
            'Incorrect: decision=HUMAN_REQUIRED, next_task_prompt="..."',
            prompt,
        )
        self.assertIn(
            "Correct: decision=HUMAN_REQUIRED, next_task_prompt=null",
            prompt,
        )

    def test_impact_prompt_exposes_exact_control_ids(self):
        state = make_project_state()
        _, prompt = build_impact_analysis_prompt(
            ImpactAnalysisRequest(state, state.change_requests[0])
        )

        self.assertIn('Existing Requirement IDs: ["req-2", "req-1"]', prompt)
        self.assertIn('Active Plan Requirement IDs: ["req-2", "req-1"]', prompt)
        self.assertIn('Existing Milestone IDs: ["milestone-1"]', prompt)
        self.assertIn('Existing Task IDs: ["task-1"]', prompt)
        self.assertIn('Active Plan Task IDs: ["task-1"]', prompt)
        self.assertIn("affected_requirement_ids may contain only existing", prompt)
        self.assertIn("affected_task_ids may contain only existing", prompt)
        self.assertIn("put new Task proposals only in tasks_to_add", prompt)
        self.assertIn("must not appear in any affected_*_ids field", prompt)
        self.assertIn('affected_task_ids = ["TASK-1"]', prompt)
        self.assertIn('{"id": "TASK-4", ...}', prompt)
        self.assertIn('affected_task_ids = ["TASK-4"]', prompt)
        self.assertIn("must use an exact ID", prompt)
        self.assertIn("Never put titles, explanations", prompt)
        self.assertIn("Preserve completed work", prompt)
        self.assertIn("requirements_to_update", prompt)
        self.assertIn("milestone_ids_reused", prompt)
        self.assertIn("milestones field contains only", prompt)
        self.assertIn("incorrect new milestone id: M1", prompt)
        self.assertIn("fresh ID such as M2", prompt)
        self.assertIn("task_requirement_updates explicitly maps", prompt)
        self.assertIn("remain unchanged unless", prompt)
        self.assertIn("never infer Task traceability", prompt)
        self.assertIn("must belong to the replacement Plan", prompt)

    def test_history_bounds_and_original_order_are_preserved(self):
        state = make_project_state()
        base_decision = state.decisions[0]
        base_report = state.execution_reports[0]
        base_event = state.events[0]
        decisions = tuple(
            replace(base_decision, id=f"decision-{index:02d}")
            for index in range(12)
        )
        reports = tuple(
            replace(base_report, id=f"report-{index:02d}")
            for index in range(12)
        )
        events = tuple(
            replace(base_event, id=f"event-{index:02d}")
            for index in range(25)
        )
        state = replace(
            state,
            decisions=decisions,
            execution_reports=reports,
            events=events,
        )

        _, prompt = build_progress_report_prompt(ProgressReportRequest(state, None))

        self.assertNotIn('id="decision-00"', prompt)
        self.assertNotIn('id="decision-01"', prompt)
        self.assertIn('id="decision-02"', prompt)
        self.assertIn('id="decision-11"', prompt)
        self.assertLess(prompt.index('id="decision-02"'), prompt.index('id="decision-11"'))
        self.assertNotIn('id="report-01"', prompt)
        self.assertIn('id="report-02"', prompt)
        self.assertNotIn('id="event-04"', prompt)
        self.assertIn('id="event-05"', prompt)
        self.assertIn('id="event-24"', prompt)
        self.assertLess(prompt.index('id="event-05"'), prompt.index('id="event-24"'))
        self.assertIn("Historical entries may be truncated", prompt)

    def test_shared_policy_enforces_truthfulness_and_human_gate_boundary(self):
        required_policy = (
            "ProjectState is the source of truth",
            "Do not claim tests were executed unless evidence exists",
            "assume missing evidence is PASS",
            "cannot approve Human Gates",
            "git push",
            "force push",
            "GitHub Release",
            "destructive file deletion",
            "destructive migration",
            "deployment",
            "secret/API key usage",
            "paid external API",
            "irreversible external side effect",
            "recommend HUMAN_REQUIRED",
            "Enforcement belongs to the Orchestrator",
            "tasks must be independently reviewable",
            "trace to at least one proposed or existing requirement",
            "Human Gate operations must never be proposed as silently automatic",
        )
        for phrase in required_policy:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, SUPERVISOR_SYSTEM_POLICY)

    def test_prompt_generation_does_not_mutate_project_state(self):
        state = make_project_state()
        original = replace(state)
        build_plan_prompt(PlanRequest(state, "Plan"))
        build_review_prompt(ReviewRequest(state, state.tasks[0], state.execution_reports[0]))
        build_impact_analysis_prompt(ImpactAnalysisRequest(state, state.change_requests[0]))
        build_progress_report_prompt(ProgressReportRequest(state, None))
        self.assertEqual(state, original)


if __name__ == "__main__":
    unittest.main()
