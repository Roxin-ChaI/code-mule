from dataclasses import replace
import unittest

from code_mule.domain.enums import PlanStatus, ProjectStatus, RequirementStatus, TaskStatus
from code_mule.domain.models import Plan
from code_mule.planning import (
    PlanMaterializationError,
    PlanMaterializer,
    PlanProposalValidator,
)

from planning.test_validation import NOW, empty_state, valid_proposal


class PlanMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.materializer = PlanMaterializer(PlanProposalValidator())
        self.state = empty_state(status=ProjectStatus.PLANNING)

    def test_materializes_all_entities_in_proposal_order(self):
        result = self.materializer.materialize(
            self.state,
            valid_proposal(),
            plan_id="plan-generated",
            operation_time=NOW,
        )

        self.assertIs(result.project.status, ProjectStatus.RUNNING)
        self.assertEqual(result.project.active_plan_id, "plan-generated")
        self.assertIsNone(result.project.current_task_id)
        self.assertEqual(tuple(item.id for item in result.requirements), ("REQ-001",))
        self.assertIs(result.requirements[0].status, RequirementStatus.ACTIVE)
        self.assertEqual(result.requirements[0].introduced_by, "boss")
        self.assertEqual(result.plans[0].requirement_ids, ("REQ-001",))
        self.assertEqual(result.plans[0].milestone_ids, ("M1",))
        self.assertIs(result.plans[0].status, PlanStatus.ACTIVE)
        self.assertEqual(result.milestones[0].task_ids, ("T1", "T2"))
        self.assertEqual(tuple(item.id for item in result.tasks), ("T1", "T2"))
        self.assertTrue(all(item.status is TaskStatus.PENDING for item in result.tasks))
        self.assertEqual(result.tasks[0].requirement_ids, ("REQ-001",))
        self.assertEqual(result.tasks[1].dependencies, ("T1",))

    def test_plan_version_uses_maximum_history_not_tuple_position(self):
        history = (
            Plan("old-4", "project-1", 4, PlanStatus.SUPERSEDED, (), (), NOW),
            Plan("old-2", "project-1", 2, PlanStatus.SUPERSEDED, (), (), NOW),
        )
        result = self.materializer.materialize(
            replace(self.state, plans=history),
            valid_proposal(),
            plan_id="plan-generated",
            operation_time=NOW,
        )
        self.assertEqual(result.plans[-1].version, 5)

    def test_invalid_state_and_plan_identity_fail_closed(self):
        for state in (
            empty_state(status=ProjectStatus.IDLE),
            empty_state(status=ProjectStatus.PLANNING, active_plan_id="existing"),
        ):
            with self.subTest(state=state.project):
                with self.assertRaises(PlanMaterializationError):
                    self.materializer.materialize(
                        state,
                        valid_proposal(),
                        plan_id="plan-generated",
                        operation_time=NOW,
                    )
        for plan_id in ("", "T1", "project-1"):
            with self.subTest(plan_id=plan_id):
                with self.assertRaises(PlanMaterializationError):
                    self.materializer.materialize(
                        self.state,
                        valid_proposal(),
                        plan_id=plan_id,
                        operation_time=NOW,
                    )


if __name__ == "__main__":
    unittest.main()
