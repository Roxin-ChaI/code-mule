from dataclasses import replace
import unittest

from code_mule.domain.enums import (
    ChangeRequestStatus,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    TaskStatus,
)
from code_mule.domain.models import Plan
from code_mule.replanning import (
    ChangeReplanMaterializer,
    ChangeReplanValidator,
    ReplanMaterializationError,
)
from code_mule.supervisor import (
    MilestoneProposal,
    RequirementProposal,
    RequirementUpdateProposal,
)

from replanning.test_validation import NOW, state, valid_proposal


def replanning_state():
    original = state()
    change = replace(
        original.change_requests[0], status=ChangeRequestStatus.ANALYZING
    )
    return replace(
        original,
        project=replace(original.project, status=ProjectStatus.REPLANNING),
        change_requests=(change,),
    )


class ChangeReplanMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.state = replanning_state()
        self.change = self.state.change_requests[0]
        self.materializer = ChangeReplanMaterializer(ChangeReplanValidator())

    def test_materializes_versioned_plan_and_preserves_completed_work(self):
        result = self.materializer.materialize(
            self.state,
            self.change,
            valid_proposal(),
            plan_id="PLAN-2",
            operation_time=NOW,
        )

        self.assertIs(result.project.status, ProjectStatus.RUNNING)
        self.assertEqual(result.project.active_plan_id, "PLAN-2")
        self.assertEqual(
            tuple(item.status for item in result.plans),
            (PlanStatus.SUPERSEDED, PlanStatus.ACTIVE),
        )
        self.assertEqual(result.plans[-1].version, 2)
        self.assertEqual(result.milestones[0].plan_id, "PLAN-1")
        self.assertEqual(result.milestones[-1].plan_id, "PLAN-2")
        tasks = {item.id: item for item in result.tasks}
        self.assertIs(tasks["T1"].status, TaskStatus.COMPLETED)
        self.assertEqual(tasks["T1"].execution_attempts, 1)
        self.assertIs(tasks["T2"].status, TaskStatus.PENDING)
        self.assertIs(tasks["T3"].status, TaskStatus.PENDING)
        self.assertTrue(all(tasks[item].milestone_id == "M2" for item in ("T1", "T2", "T3")))
        self.assertIs(
            result.change_requests[0].status, ChangeRequestStatus.APPLIED
        )
        self.assertEqual(result.impact_analyses[-1].tasks_to_add, ("T3",))

    def test_explicit_reopen_changes_only_named_completed_task(self):
        proposal = replace(
            valid_proposal(),
            affected_task_ids=("T1",),
            affected_completed_tasks=("T1",),
            tasks_to_reopen=("T1",),
        )
        result = self.materializer.materialize(
            self.state,
            self.change,
            proposal,
            plan_id="PLAN-2",
            operation_time=NOW,
        )
        tasks = {item.id: item for item in result.tasks}
        self.assertIs(tasks["T1"].status, TaskStatus.REOPENED)
        self.assertIs(tasks["T2"].status, TaskStatus.PENDING)

    def test_requirement_update_preserves_old_and_links_replacement(self):
        proposal = replace(
            valid_proposal(),
            affected_requirement_ids=("REQ-BASE",),
            affected_task_ids=("T2",),
            affected_pending_tasks=("T2",),
            requirements_to_add=(),
            requirements_to_update=(
                RequirementUpdateProposal(
                    "REQ-BASE",
                    RequirementProposal(
                        "REQ-BASE-V2",
                        "Calculator v2",
                        "Add multiplication.",
                        "high",
                        ("all operations pass",),
                    ),
                ),
            ),
            tasks_to_cancel=("T2",),
            tasks_to_add=(
                replace(
                    valid_proposal().tasks_to_add[0],
                    requirement_ids=("REQ-BASE-V2",),
                ),
            ),
            milestones=(MilestoneProposal("M2", "Changed", ("T1", "T3")),),
        )
        result = self.materializer.materialize(
            self.state,
            self.change,
            proposal,
            plan_id="PLAN-2",
            operation_time=NOW,
        )
        requirements = {item.id: item for item in result.requirements}
        self.assertIs(
            requirements["REQ-BASE"].status, RequirementStatus.SUPERSEDED
        )
        self.assertEqual(
            requirements["REQ-BASE-V2"].supersedes_id, "REQ-BASE"
        )
        self.assertEqual(result.plans[-1].requirement_ids, ("REQ-BASE-V2",))
        self.assertIs(
            next(item for item in result.tasks if item.id == "T2").status,
            TaskStatus.CANCELLED,
        )

    def test_version_uses_maximum_history_and_plan_id_is_new(self):
        historical = Plan(
            "PLAN-7",
            "project-1",
            7,
            PlanStatus.SUPERSEDED,
            (),
            (),
            NOW,
        )
        current = replace(self.state, plans=(historical,) + self.state.plans)
        result = self.materializer.materialize(
            current,
            self.change,
            valid_proposal(),
            plan_id="PLAN-8",
            operation_time=NOW,
        )
        self.assertEqual(result.plans[-1].version, 8)

        with self.assertRaises(ReplanMaterializationError):
            self.materializer.materialize(
                self.state,
                self.change,
                valid_proposal(),
                plan_id="T3",
                operation_time=NOW,
            )


if __name__ == "__main__":
    unittest.main()
