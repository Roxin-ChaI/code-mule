import copy
import unittest
from unittest.mock import patch

from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    UnsupportedStateSchema,
    deserialize_project_state,
    serialize_project_state,
)

from state import make_project_state


_V2_IMPACT_FIELDS = (
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


def historical_payload(version: int) -> dict[str, object]:
    payload = copy.deepcopy(serialize_project_state(make_project_state()))
    payload["schema_version"] = version
    project = payload["project"]

    if version <= 10:
        for report in payload["execution_reports"]:
            report.pop("verification_checks")

    if version <= 9:
        for report in payload["execution_reports"]:
            report["human_action_required"] = report.pop("human_action") is not None

    if version <= 7:
        project.pop("objective")
        payload.pop("project_verification_spec")
        payload.pop("project_verification_results")
    if version <= 6:
        payload.pop("git_baselines")
        payload.pop("git_change_sets")
        payload.pop("git_commit_results")
    if version <= 5:
        payload.pop("execution_leases")
    if version <= 4:
        payload.pop("human_actions")
        payload.pop("human_resolutions")
    if version <= 3:
        project.pop("workspace")
    if version <= 2:
        for requirement in payload["requirements"]:
            requirement.pop("supersedes_id")
        for impact in payload["impact_analyses"]:
            for field in _V2_IMPACT_FIELDS:
                impact.pop(field)
    if version <= 1:
        for task in payload["tasks"]:
            task.pop("requirement_ids")
    return payload


class HistoricalMigrationMatrixTests(unittest.TestCase):
    def test_every_historical_schema_loads_without_reordering(self):
        for version in range(1, CURRENT_SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                payload = historical_payload(version)
                original = copy.deepcopy(payload)

                with patch("socket.create_connection") as network, patch(
                    "subprocess.run"
                ) as process:
                    restored = deserialize_project_state(payload)

                self.assertEqual(payload, original)
                self.assertFalse(network.called)
                self.assertFalse(process.called)
                self.assertEqual(
                    tuple(item.id for item in restored.requirements),
                    ("req-2", "req-1"),
                )
                self.assertEqual(
                    tuple(item.id for item in restored.tasks),
                    ("task-1",),
                )
                self.assertEqual(
                    tuple(item.id for item in restored.events),
                    ("event-1",),
                )

                current = serialize_project_state(restored)
                self.assertEqual(current["schema_version"], CURRENT_SCHEMA_VERSION)
                self.assertEqual(deserialize_project_state(current), restored)

    def test_unknown_past_and_future_schemas_fail_closed(self):
        for version in (0, CURRENT_SCHEMA_VERSION + 1, 999):
            with self.subTest(version=version):
                payload = serialize_project_state(make_project_state())
                payload["schema_version"] = version
                with self.assertRaises(UnsupportedStateSchema):
                    deserialize_project_state(payload)


if __name__ == "__main__":
    unittest.main()
