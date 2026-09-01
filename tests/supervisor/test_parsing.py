import copy
import unittest

from code_mule.domain.enums import SupervisorDecisionType
from code_mule.supervisor.parsing import (
    InvalidSupervisorResponse,
    parse_impact_analysis_response,
    parse_plan_response,
    parse_progress_report_response,
    parse_review_response,
)
from code_mule.supervisor.schemas import (
    impact_analysis_response_schema,
    plan_response_schema,
    progress_report_response_schema,
    review_response_schema,
)


def task_payload():
    return {
        "id": "task-1",
        "title": "Implement",
        "description": "Implement the contract",
        "dependencies": ["task-0"],
        "acceptance_criteria": ["tests pass"],
        "requirement_ids": ["req-2"],
    }


def plan_payload():
    return {
        "summary": "Plan summary",
        "requirements": [
            {
                "id": "req-new",
                "title": "New requirement",
                "description": "Deliver the new behavior",
                "priority": "high",
                "acceptance_criteria": ["behavior is verified"],
            }
        ],
        "requirements_considered": ["req-2", "req-1"],
        "milestones": [
            {"id": "milestone-1", "title": "Core", "task_ids": ["task-1"]}
        ],
        "tasks": [task_payload()],
        "risks": ["risk-b", "risk-a"],
        "rationale": "Structured delivery",
    }


def impact_payload():
    return {
        "change_request_id": "change-1",
        "summary": "Apply the requested boundary change",
        "architecture_impact": "boundary change",
        "affected_components": ["supervisor"],
        "affected_requirement_ids": ["req-2"],
        "affected_task_ids": ["task-0", "task-1", "task-2"],
        "affected_completed_tasks": ["task-0"],
        "affected_in_progress_tasks": ["task-1"],
        "affected_pending_tasks": ["task-2"],
        "requirements_to_add": [
            {
                "id": "req-new",
                "title": "New requirement",
                "description": "Deliver new behavior",
                "priority": "high",
                "acceptance_criteria": ["behavior is verified"],
            }
        ],
        "requirements_to_update": [
            {
                "supersedes_id": "req-2",
                "requirement": {
                    "id": "req-2-v2",
                    "title": "Updated requirement",
                    "description": "Replace old behavior",
                    "priority": "high",
                    "acceptance_criteria": ["replacement is verified"],
                },
            }
        ],
        "tasks_to_add": [task_payload()],
        "tasks_to_reopen": ["task-0"],
        "tasks_to_cancel": ["task-old"],
        "milestone_ids_reused": ["milestone-1"],
        "milestones": [
            {"id": "milestone-v2", "title": "Changed plan", "task_ids": ["task-0", "task-1"]}
        ],
        "dependency_changes": [
            {"task_id": "task-1", "dependencies": ["task-0"]}
        ],
        "risks": ["integration risk"],
        "recommendation": "replan",
        "rationale": "scope changed",
    }


def progress_payload():
    return {
        "summary": "In progress",
        "current_status": "running",
        "current_work": "task-1",
        "completed": ["task-0"],
        "remaining": ["task-2"],
        "blockers": [],
        "risks": ["risk-1"],
        "quality_summary": None,
    }


class SchemaTests(unittest.TestCase):
    def test_schemas_are_strict_and_fresh(self):
        factories = (
            plan_response_schema,
            review_response_schema,
            impact_analysis_response_schema,
            progress_report_response_schema,
        )
        for factory in factories:
            with self.subTest(factory=factory):
                first = factory()
                second = factory()
                self.assertIsNot(first, second)
                self.assertFalse(first["additionalProperties"])
                first["mutated"] = True
                self.assertNotIn("mutated", second)

        plan = plan_response_schema()
        requirement = plan["properties"]["requirements"]["items"]
        task = plan["properties"]["tasks"]["items"]
        self.assertFalse(requirement["additionalProperties"])
        self.assertEqual(task["properties"]["requirement_ids"]["minItems"], 1)
        impact = impact_analysis_response_schema()
        self.assertIn("milestone_ids_reused", impact["required"])
        self.assertFalse(
            impact["properties"]["milestones"]["items"]["additionalProperties"]
        )


class ParsingTests(unittest.TestCase):
    def test_plan_parser_restores_nested_tuples_in_original_order(self):
        result = parse_plan_response(plan_payload())
        self.assertEqual(result.requirements_considered, ("req-2", "req-1"))
        self.assertEqual(result.requirements[0].id, "req-new")
        self.assertEqual(result.risks, ("risk-b", "risk-a"))
        self.assertEqual(result.tasks[0].dependencies, ("task-0",))
        self.assertEqual(result.tasks[0].requirement_ids, ("req-2",))
        self.assertEqual(result.milestones[0].task_ids, ("task-1",))

    def test_review_parser_supports_every_valid_decision(self):
        cases = (
            ("continue", None, SupervisorDecisionType.CONTINUE),
            ("rework", "repair it", SupervisorDecisionType.REWORK),
            ("human_required", None, SupervisorDecisionType.HUMAN_REQUIRED),
            ("done", None, SupervisorDecisionType.DONE),
        )
        for decision, prompt, expected in cases:
            with self.subTest(decision=decision):
                result = parse_review_response(
                    {
                        "decision": decision,
                        "rationale": "because",
                        "next_task_prompt": prompt,
                        "issues": ["b", "a"],
                    }
                )
                self.assertIs(result.decision, expected)
                self.assertEqual(result.issues, ("b", "a"))

    def test_impact_and_progress_parsers_return_typed_results(self):
        impact = parse_impact_analysis_response(impact_payload())
        progress = parse_progress_report_response(progress_payload())
        self.assertEqual(impact.tasks_to_add[0].id, "task-1")
        self.assertEqual(impact.tasks_to_reopen, ("task-0",))
        self.assertEqual(
            impact.requirements_to_update[0].supersedes_id, "req-2"
        )
        self.assertEqual(impact.milestones[0].id, "milestone-v2")
        self.assertEqual(impact.milestone_ids_reused, ("milestone-1",))
        self.assertEqual(impact.dependency_changes[0].task_id, "task-1")
        self.assertEqual(progress.completed, ("task-0",))
        self.assertIsNone(progress.quality_summary)

    def test_missing_extra_wrong_and_null_fields_are_rejected(self):
        cases = []
        missing = plan_payload()
        missing.pop("summary")
        cases.append(missing)
        extra = plan_payload()
        extra["unexpected"] = True
        cases.append(extra)
        wrong = plan_payload()
        wrong["risks"] = "not-an-array"
        cases.append(wrong)
        null_required = plan_payload()
        null_required["rationale"] = None
        cases.append(null_required)
        tuple_instead_of_array = plan_payload()
        tuple_instead_of_array["risks"] = ("risk",)
        cases.append(tuple_instead_of_array)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidSupervisorResponse):
                    parse_plan_response(payload)

    def test_malformed_nested_task_is_rejected(self):
        payload = impact_payload()
        payload["tasks_to_add"][0].pop("title")
        with self.assertRaises(InvalidSupervisorResponse):
            parse_impact_analysis_response(payload)

    def test_invalid_review_decision_and_invariants_are_rejected(self):
        payload = {
            "decision": "invalid",
            "rationale": "because",
            "next_task_prompt": None,
            "issues": [],
        }
        with self.assertRaises(InvalidSupervisorResponse):
            parse_review_response(payload)

        for decision, prompt in (
            ("rework", None),
            ("human_required", "not allowed"),
            ("done", "not allowed"),
        ):
            invalid = copy.deepcopy(payload)
            invalid["decision"] = decision
            invalid["next_task_prompt"] = prompt
            with self.subTest(decision=decision):
                with self.assertRaises(InvalidSupervisorResponse):
                    parse_review_response(invalid)


if __name__ == "__main__":
    unittest.main()
