import copy
import unittest

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
        requirements = payload["requirements"]
        self.assertIsInstance(requirements, list)
        self.assertEqual([item["id"] for item in requirements], ["req-2", "req-1"])
        self.assertIsInstance(requirements[0]["acceptance_criteria"], list)
        tasks = payload["tasks"]
        self.assertEqual(tasks[0]["status"], TaskStatus.IN_PROGRESS.value)

    def test_deserialize_restores_tuples_enums_and_datetimes(self):
        original = make_project_state()
        restored = deserialize_project_state(serialize_project_state(original))

        self.assertIsInstance(restored.requirements, tuple)
        self.assertIsInstance(restored.requirements[0].acceptance_criteria, tuple)
        self.assertIs(restored.project.status, ProjectStatus.RUNNING)
        self.assertIs(restored.tasks[0].status, TaskStatus.IN_PROGRESS)
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


class InvalidProjectStateTests(unittest.TestCase):
    def setUp(self):
        self.payload = serialize_project_state(make_project_state())

    def test_missing_schema_version_is_unsupported(self):
        self.payload.pop("schema_version")
        with self.assertRaises(UnsupportedStateSchema):
            deserialize_project_state(self.payload)

    def test_unsupported_schema_version_is_rejected(self):
        self.payload["schema_version"] = 2
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

    def test_collection_with_wrong_type_is_invalid(self):
        self.payload["requirements"] = {}
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(self.payload)

    def test_top_level_payload_with_wrong_type_is_invalid(self):
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state([])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
