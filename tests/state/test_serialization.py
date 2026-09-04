import copy
import unittest
from dataclasses import replace

from code_mule.domain.enums import ProjectStatus, TaskStatus
from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    InvalidProjectState,
    UnsupportedStateSchema,
    deserialize_project_state,
    serialize_project_state,
)

from state import make_project_state, make_project_state_without_quality


class ProjectStateSerializationTests(unittest.TestCase):
    def test_complete_state_round_trip(self):
        state = make_project_state()
        self.assertEqual(deserialize_project_state(serialize_project_state(state)), state)

    def test_serialized_format_is_json_compatible_and_preserves_order(self):
        state = make_project_state()
        payload = serialize_project_state(state)

        self.assertEqual(payload["schema_version"], CURRENT_SCHEMA_VERSION)
        project = payload["project"]
        self.assertIsInstance(project, dict)
        self.assertEqual(project["status"], ProjectStatus.RUNNING.value)
        self.assertEqual(project["created_at"], state.project.created_at.isoformat())
        self.assertIsNone(project["workspace"])
        requirements = payload["requirements"]
        self.assertIsInstance(requirements, list)
        self.assertEqual([item["id"] for item in requirements], ["req-2", "req-1"])
        self.assertIsInstance(requirements[0]["acceptance_criteria"], list)
        self.assertEqual(requirements[0]["supersedes_id"], "req-1")
        tasks = payload["tasks"]
        self.assertEqual(tasks[0]["status"], TaskStatus.IN_PROGRESS.value)
        self.assertEqual(tasks[0]["requirement_ids"], ["req-2"])

    def test_deserialize_restores_tuples_enums_and_datetimes(self):
        original = make_project_state()
        restored = deserialize_project_state(serialize_project_state(original))

        self.assertIsInstance(restored.requirements, tuple)
        self.assertIsInstance(restored.requirements[0].acceptance_criteria, tuple)
        self.assertEqual(restored.requirements[0].supersedes_id, "req-1")
        self.assertIs(restored.project.status, ProjectStatus.RUNNING)
        self.assertIs(restored.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(restored.tasks[0].requirement_ids, ("req-2",))
        self.assertEqual(restored.project.created_at, original.project.created_at)

    def test_quality_status_value_round_trip(self):
        state = make_project_state()
        restored = deserialize_project_state(serialize_project_state(state))
        self.assertEqual(restored.quality_status, state.quality_status)

    def test_quality_status_none_round_trip(self):
        state = make_project_state_without_quality()
        restored = deserialize_project_state(serialize_project_state(state))
        self.assertIsNone(restored.quality_status)
        self.assertEqual(restored, state)

    def test_cancellation_statuses_round_trip_without_schema_shape_change(self):
        for status in (ProjectStatus.CANCEL_REQUESTED, ProjectStatus.CANCELLED):
            with self.subTest(status=status):
                state = make_project_state()
                state = replace(state, project=replace(state.project, status=status))
                payload = serialize_project_state(state)
                self.assertEqual(payload["schema_version"], 8)
                self.assertIs(deserialize_project_state(payload).project.status, status)


class InvalidProjectStateTests(unittest.TestCase):
    def setUp(self):
        self.payload = serialize_project_state(make_project_state())

    def test_missing_schema_version_is_unsupported(self):
        self.payload.pop("schema_version")
        with self.assertRaises(UnsupportedStateSchema):
            deserialize_project_state(self.payload)

    def test_unsupported_schema_version_is_rejected(self):
        self.payload["schema_version"] = CURRENT_SCHEMA_VERSION + 1
        with self.assertRaises(UnsupportedStateSchema):
            deserialize_project_state(self.payload)

    def test_wrong_schema_version_type_is_rejected(self):
        self.payload["schema_version"] = "1"
        with self.assertRaises(UnsupportedStateSchema):
            deserialize_project_state(self.payload)

    def test_missing_project_is_invalid(self):
        self.payload.pop("project")
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(self.payload)

    def test_malformed_enum_is_invalid(self):
        payload = copy.deepcopy(self.payload)
        payload["project"]["status"] = "not-a-status"
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(payload)

    def test_malformed_datetime_is_invalid(self):
        payload = copy.deepcopy(self.payload)
        payload["project"]["created_at"] = "not-a-datetime"
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(payload)

    def test_invalid_plan_version_is_invalid(self):
        payload = copy.deepcopy(self.payload)
        payload["plans"][0]["version"] = 0
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(payload)

    def test_invalid_task_execution_attempts_is_invalid(self):
        payload = copy.deepcopy(self.payload)
        payload["tasks"][0]["execution_attempts"] = -1
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(payload)

    def test_v1_state_migrates_task_traceability_without_reordering(self):
        payload = copy.deepcopy(self.payload)
        payload["schema_version"] = 1
        for task in payload["tasks"]:
            task.pop("requirement_ids")
        original = copy.deepcopy(payload)

        restored = deserialize_project_state(payload)

        self.assertEqual(payload, original)
        self.assertEqual(tuple(task.id for task in restored.tasks), ("task-1",))
        self.assertEqual(restored.tasks[0].requirement_ids, ())
        self.assertEqual(
            tuple(requirement.id for requirement in restored.requirements),
            ("req-2", "req-1"),
        )

    def test_v2_state_migrates_replacement_metadata(self):
        payload = copy.deepcopy(self.payload)
        payload["schema_version"] = 2
        for requirement in payload["requirements"]:
            requirement.pop("supersedes_id")
        new_impact_fields = (
            "summary",
            "affected_requirement_ids",
            "affected_task_ids",
            "requirements_to_add",
            "requirements_to_update",
            "milestone_ids",
            "dependency_changes",
            "risks",
            "rationale",
        )
        for impact in payload["impact_analyses"]:
            for field in new_impact_fields:
                impact.pop(field)

        restored = deserialize_project_state(payload)

        self.assertTrue(
            all(item.supersedes_id is None for item in restored.requirements)
        )
        self.assertEqual(restored.impact_analyses[0].affected_task_ids, ())

    def test_v3_task_requirement_ids_are_required_and_typed(self):
        missing = copy.deepcopy(self.payload)
        missing["tasks"][0].pop("requirement_ids")
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(missing)

        invalid = copy.deepcopy(self.payload)
        invalid["tasks"][0]["requirement_ids"] = ["req-2", 1]
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(invalid)

    def test_v3_state_migrates_with_unknown_workspace(self):
        payload = copy.deepcopy(self.payload)
        payload["schema_version"] = 3
        payload["project"].pop("workspace")

        restored = deserialize_project_state(payload)

        self.assertIsNone(restored.project.workspace)

    def test_collection_with_wrong_type_is_invalid(self):
        self.payload["requirements"] = {}
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(self.payload)

    def test_top_level_payload_with_wrong_type_is_invalid(self):
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state([])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
