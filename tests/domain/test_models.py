import unittest
from datetime import datetime, timezone

from code_mule.domain.enums import (
    ChangeRequestStatus,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import (
    ChangeRequest,
    Decision,
    ExecutionReport,
    ImpactAnalysis,
    Milestone,
    Plan,
    Project,
    ProjectEvent,
    QualityStatus,
    Requirement,
    Task,
)


CREATED = datetime(2026, 8, 30, tzinfo=timezone.utc)
UPDATED = datetime(2026, 8, 31, tzinfo=timezone.utc)


def make_requirement(**overrides):
    values = {
        "id": "req-1",
        "project_id": "project-1",
        "title": "Define contracts",
        "description": "Define domain contracts.",
        "status": RequirementStatus.ACTIVE,
        "priority": "high",
        "acceptance_criteria": ("criteria-a", "criteria-b"),
        "introduced_by": "boss",
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    values.update(overrides)
    return Requirement(**values)


def make_task(**overrides):
    values = {
        "id": "task-1",
        "milestone_id": "milestone-1",
        "title": "Implement contracts",
        "description": "Implement the typed contracts.",
        "status": TaskStatus.PENDING,
        "dependencies": ("task-0",),
        "acceptance_criteria": ("tests pass",),
        "execution_attempts": 0,
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    values.update(overrides)
    return Task(**values)


class ModelContractTests(unittest.TestCase):
    def test_valid_construction_and_explicit_values_are_preserved(self):
        requirement = make_requirement()
        task = make_task()
        project = Project("project-1", "Code Mule", ProjectStatus.IDLE, None, None, CREATED, UPDATED)
        plan = Plan("plan-1", "project-1", 1, PlanStatus.ACTIVE, ("req-1",), ("milestone-1",), CREATED)
        milestone = Milestone("milestone-1", "plan-1", "Contracts", "pending", ("task-1",))
        change = ChangeRequest("change-1", "project-1", "Add a field", ChangeRequestStatus.PENDING, ("req-1",), "boss", CREATED)
        impact = ImpactAnalysis("change-1", "none", ("domain",), ("task-0",), ("task-1",), (), ("task-2",), (), (), "replan")
        decision = Decision("decision-1", "task-1", SupervisorDecisionType.CONTINUE, "Passed", CREATED)
        report = ExecutionReport("report-1", "task-1", 1, "passed", ("a.py",), ("ok",), ("ok",), "clean", (), False, "Done", CREATED)
        quality = QualityStatus("passed", "not_run", "not_run", "not_run", True)
        event = ProjectEvent("event-1", "project-1", "task.completed", "task-1", CREATED, {"source": "test"})

        self.assertEqual(requirement.acceptance_criteria, ("criteria-a", "criteria-b"))
        self.assertEqual(task.dependencies, ("task-0",))
        self.assertEqual(task.acceptance_criteria, ("tests pass",))
        self.assertEqual(project.created_at, CREATED)
        self.assertEqual(project.updated_at, UPDATED)
        self.assertEqual(plan.version, 1)
        self.assertEqual(milestone.task_ids, ("task-1",))
        self.assertEqual(change.affected_requirement_ids, ("req-1",))
        self.assertEqual(impact.affected_components, ("domain",))
        self.assertEqual(decision.task_id, "task-1")
        self.assertEqual(report.attempt, 1)
        self.assertTrue(quality.repository_clean)
        self.assertEqual(event.metadata, {"source": "test"})

    def test_plan_version_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            Plan("plan-1", "project-1", 0, PlanStatus.ACTIVE, (), (), CREATED)

    def test_negative_execution_attempts_are_rejected(self):
        with self.assertRaises(ValueError):
            make_task(execution_attempts=-1)

    def test_empty_required_ids_titles_and_name_are_rejected(self):
        for field in ("id", "project_id", "title"):
            with self.subTest(model="requirement", field=field):
                with self.assertRaises(ValueError):
                    make_requirement(**{field: ""})
        for field in ("id", "milestone_id", "title"):
            with self.subTest(model="task", field=field):
                with self.assertRaises(ValueError):
                    make_task(**{field: ""})
        with self.assertRaises(ValueError):
            Project("", "Code Mule", ProjectStatus.IDLE, None, None, CREATED, UPDATED)
        with self.assertRaises(ValueError):
            Project("project-1", "", ProjectStatus.IDLE, None, None, CREATED, UPDATED)

    def test_models_do_not_generate_ids_or_timestamps(self):
        requirement = make_requirement(id="provided", created_at=CREATED, updated_at=UPDATED)
        self.assertEqual(requirement.id, "provided")
        self.assertIs(requirement.created_at, CREATED)
        self.assertIs(requirement.updated_at, UPDATED)


if __name__ == "__main__":
    unittest.main()
