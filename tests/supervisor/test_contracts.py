import unittest

from code_mule.domain.enums import SupervisorDecisionType
from code_mule.supervisor.contracts import (
    ImpactAnalysisRequest,
    ImpactAnalysisResult,
    MilestoneProposal,
    PlanProposal,
    PlanRequest,
    ProgressReportRequest,
    RequirementProposal,
    RequirementUpdateProposal,
    ReviewRequest,
    ReviewResult,
    SupervisorOperation,
    TaskDependencyChange,
    TaskRequirementUpdate,
    TaskProposal,
)

from state import make_project_state


class SupervisorContractTests(unittest.TestCase):
    def test_operation_values(self):
        self.assertEqual(
            {item.name: item.value for item in SupervisorOperation},
            {
                "PLAN": "plan",
                "REVIEW": "review",
                "IMPACT_ANALYSIS": "impact_analysis",
                "PROGRESS_REPORT": "progress_report",
            },
        )

    def test_requests_preserve_explicit_domain_references(self):
        state = make_project_state()
        plan = PlanRequest(state, "Deliver Phase 4")
        review = ReviewRequest(state, state.tasks[0], state.execution_reports[0])
        impact = ImpactAnalysisRequest(state, state.change_requests[0])
        progress = ProgressReportRequest(state, None)

        self.assertIs(plan.project_state, state)
        self.assertIs(review.project_state, state)
        self.assertIs(review.task, state.tasks[0])
        self.assertIs(review.execution_report, state.execution_reports[0])
        self.assertIs(impact.change_request, state.change_requests[0])
        self.assertIs(progress.project_state, state)

    def test_request_text_validation_does_not_strip(self):
        state = make_project_state()
        with self.assertRaises(ValueError):
            PlanRequest(state, "")
        with self.assertRaises(ValueError):
            ProgressReportRequest(state, "")
        self.assertEqual(PlanRequest(state, " ").objective, " ")
        self.assertEqual(ProgressReportRequest(state, " ").question, " ")

    def test_proposal_validation_and_order_preservation(self):
        requirement = RequirementProposal(
            "req-new", "Requirement", "Description", "high", ("verified",)
        )
        task = TaskProposal(
            "task-1", "Title", "Description", ("b", "a"), ("z", "y"),
            ("req-new",),
        )
        milestone = MilestoneProposal("milestone-1", "Milestone", ("task-2", "task-1"))
        proposal = PlanProposal(
            "Summary", (requirement,), ("req-2", "req-1"), (milestone,),
            (task,), ("risk",), "Rationale"
        )
        self.assertEqual(task.dependencies, ("b", "a"))
        self.assertEqual(milestone.task_ids, ("task-2", "task-1"))
        self.assertEqual(proposal.requirements_considered, ("req-2", "req-1"))

        for field in ("id", "title", "description"):
            values = {"id": "task", "title": "title", "description": "description", "dependencies": (), "acceptance_criteria": ("verified",), "requirement_ids": ("req",)}
            values[field] = ""
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    TaskProposal(**values)
        with self.assertRaises(ValueError):
            MilestoneProposal("", "title", ())
        with self.assertRaises(ValueError):
            PlanProposal("", (), (), (), (), (), "rationale")
        with self.assertRaises(ValueError):
            RequirementProposal("req", "title", "description", "high", ())
        with self.assertRaises(ValueError):
            TaskProposal("task", "title", "description", (), ("done",), ())

    def test_review_result_invariants(self):
        ReviewResult(SupervisorDecisionType.CONTINUE, "ok", None, ())
        ReviewResult(SupervisorDecisionType.CONTINUE, "ok", "next", ())
        ReviewResult(SupervisorDecisionType.REWORK, "fix", "repair", ())
        ReviewResult(SupervisorDecisionType.HUMAN_REQUIRED, "approval", None, ())
        ReviewResult(SupervisorDecisionType.DONE, "done", None, ())

        with self.assertRaises(ValueError):
            ReviewResult(SupervisorDecisionType.REWORK, "fix", None, ())
        with self.assertRaises(ValueError):
            ReviewResult(SupervisorDecisionType.REWORK, "fix", "", ())
        with self.assertRaises(ValueError):
            ReviewResult(SupervisorDecisionType.HUMAN_REQUIRED, "approval", "next", ())
        with self.assertRaises(ValueError):
            ReviewResult(SupervisorDecisionType.DONE, "done", "next", ())

    def test_impact_result_requires_recommendation_and_rationale(self):
        fields = {
            "change_request_id": "change-1",
            "summary": "Apply change",
            "architecture_impact": "none",
            "affected_components": (),
            "affected_requirement_ids": (),
            "affected_task_ids": (),
            "affected_completed_tasks": (),
            "affected_in_progress_tasks": (),
            "affected_pending_tasks": (),
            "requirements_to_add": (),
            "requirements_to_update": (),
            "tasks_to_add": (),
            "tasks_to_reopen": (),
            "tasks_to_cancel": (),
            "milestone_ids_reused": (),
            "milestones": (),
            "dependency_changes": (),
            "task_requirement_updates": (),
            "risks": (),
            "recommendation": "replan",
            "rationale": "requirement changed",
        }
        ImpactAnalysisResult(**fields)
        for field in (
            "change_request_id",
            "summary",
            "recommendation",
            "rationale",
        ):
            invalid = dict(fields)
            invalid[field] = ""
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    ImpactAnalysisResult(**invalid)

        update = RequirementUpdateProposal(
            "req-old",
            RequirementProposal(
                "req-new", "Replacement", "Changed", "high", ("verified",)
            ),
        )
        dependency = TaskDependencyChange("task-1", ("task-0",))
        traceability = TaskRequirementUpdate("task-1", ("req-new",))
        self.assertEqual(update.supersedes_id, "req-old")
        self.assertEqual(dependency.dependencies, ("task-0",))
        self.assertEqual(traceability.requirement_ids, ("req-new",))


if __name__ == "__main__":
    unittest.main()
