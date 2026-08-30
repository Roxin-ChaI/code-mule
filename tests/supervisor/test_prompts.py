import unittest
from dataclasses import replace

from code_mule.supervisor.contracts import (
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
)

from state import make_project_state


class SupervisorPromptTests(unittest.TestCase):
    def test_plan_prompt_contains_current_facts_and_context(self):
        state = make_project_state()
        system_prompt, user_prompt = build_plan_prompt(
            PlanRequest(state, "Implement Phase 4")
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
        self.assertLess(user_prompt.index('id="req-2"'), user_prompt.index('id="req-1"'))
        self.assertNotIn("ProjectState(", user_prompt)
        self.assertNotIn("Requirement(", user_prompt)
        self.assertNotIn("Project(", user_prompt)

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
