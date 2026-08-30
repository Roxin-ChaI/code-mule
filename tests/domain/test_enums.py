import unittest
from enum import StrEnum

from code_mule.domain.enums import (
    BossCommandType,
    ChangeRequestStatus,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    SupervisorDecisionType,
    TaskStatus,
)


class EnumContractTests(unittest.TestCase):
    def test_project_status_members_and_values(self):
        self.assertIsInstance(ProjectStatus.IDLE, StrEnum)
        self.assertEqual(
            {member.name: member.value for member in ProjectStatus},
            {
                "IDLE": "idle",
                "PLANNING": "planning",
                "RUNNING": "running",
                "CHANGE_REQUESTED": "change_requested",
                "REPLANNING": "replanning",
                "PAUSED_BY_BOSS": "paused_by_boss",
                "HUMAN_REQUIRED": "human_required",
                "DONE": "done",
                "FAILED": "failed",
            },
        )

    def test_task_status_members_and_values(self):
        self.assertEqual(
            {member.name: member.value for member in TaskStatus},
            {
                "PENDING": "pending",
                "IN_PROGRESS": "in_progress",
                "COMPLETED": "completed",
                "BLOCKED": "blocked",
                "CANCELLED": "cancelled",
                "REOPENED": "reopened",
            },
        )

    def test_decision_and_command_values(self):
        self.assertEqual(
            {member.name: member.value for member in SupervisorDecisionType},
            {
                "CONTINUE": "continue",
                "REWORK": "rework",
                "HUMAN_REQUIRED": "human_required",
                "DONE": "done",
            },
        )
        self.assertEqual(
            {member.name: member.value for member in BossCommandType},
            {"QUERY": "query", "CHANGE": "change", "PAUSE": "pause", "RESUME": "resume"},
        )

    def test_requirement_plan_and_change_status_values(self):
        self.assertEqual(
            {member.name: member.value for member in RequirementStatus},
            {"ACTIVE": "active", "SUPERSEDED": "superseded", "REMOVED": "removed"},
        )
        self.assertEqual(
            {member.name: member.value for member in PlanStatus},
            {"ACTIVE": "active", "SUPERSEDED": "superseded", "COMPLETED": "completed"},
        )
        self.assertEqual(
            {member.name: member.value for member in ChangeRequestStatus},
            {
                "PENDING": "pending",
                "ANALYZING": "analyzing",
                "APPLIED": "applied",
                "REJECTED": "rejected",
            },
        )


if __name__ == "__main__":
    unittest.main()
