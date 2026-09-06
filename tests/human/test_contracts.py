from dataclasses import replace
from datetime import UTC, datetime
import unittest

from code_mule.domain import (
    HumanAction,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolution,
    HumanResolutionStrategy,
    ProjectStatus,
    WorkerInputDetails,
)
from code_mule.human import pending_action, request_human_action
from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    deserialize_project_state,
    serialize_project_state,
)
from state import make_project_state


NOW = datetime(2026, 9, 2, tzinfo=UTC)


class HumanActionContractTests(unittest.TestCase):
    def test_typed_action_and_resolution_require_explicit_values(self):
        action = HumanAction(
            "action-1",
            "project-1",
            "task-1",
            HumanActionCategory.WORKER_APPROVAL,
            "Approval required",
            "Approve the exact request",
            "External side effect",
            HumanActionStatus.PENDING,
            NOW,
        )
        resolution = HumanResolution(
            "resolution-1",
            action.id,
            action.project_id,
            HumanResolutionStrategy.ACKNOWLEDGE,
            "Reviewed",
            NOW,
        )
        self.assertIs(action.status, HumanActionStatus.PENDING)
        self.assertEqual(resolution.action_id, action.id)
        with self.assertRaises(ValueError):
            replace(action, status=HumanActionStatus.APPROVED)

    def test_request_is_atomic_typed_and_audited(self):
        state = make_project_state()
        identifiers = iter(("source", "requested"))
        updated = request_human_action(
            state,
            category=HumanActionCategory.RECOVERY_UNCERTAIN,
            summary="Ownership uncertain",
            requested_action="Inspect state",
            risk="Duplicate side effect",
            task_id="task-1",
            operation_time=NOW,
            action_id="action-1",
            event_id_factory=lambda: next(identifiers),
            source_event_types=("project.execution_recovery_required",),
        )
        self.assertIs(updated.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(pending_action(updated).id, "action-1")
        self.assertIn(
            "human_action.requested", tuple(event.event_type for event in updated.events)
        )
        self.assertIs(state.project.status, ProjectStatus.RUNNING)

    def test_state_v4_migrates_and_v6_round_trips_human_records(self):
        legacy = serialize_project_state(make_project_state())
        legacy["schema_version"] = 4
        for report in legacy["execution_reports"]:
            report["human_action_required"] = report.pop("human_action") is not None
        del legacy["human_actions"]
        del legacy["human_resolutions"]
        migrated = deserialize_project_state(legacy)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 12)
        self.assertEqual(migrated.human_actions, ())
        self.assertEqual(migrated.human_resolutions, ())

        identifiers = iter(("source", "requested"))
        gated = request_human_action(
            make_project_state(),
            category=HumanActionCategory.ATTEMPT_LIMIT,
            summary="Attempt limit",
            requested_action="Resolve",
            risk="Repeated work",
            task_id="task-1",
            operation_time=NOW,
            action_id="action-1",
            event_id_factory=lambda: next(identifiers),
            source_event_types=("task.cycle_limit_reached",),
        )
        self.assertEqual(
            deserialize_project_state(serialize_project_state(gated)), gated
        )

        workspace_ids = iter(("workspace-source", "workspace-requested"))
        workspace_block = request_human_action(
            make_project_state(),
            category=HumanActionCategory.WORKSPACE_BLOCK,
            summary="Workspace blocked",
            requested_action="Clean the workspace and retry",
            risk="Unrelated changes must be preserved",
            task_id="task-1",
            operation_time=NOW,
            action_id="action-workspace",
            event_id_factory=lambda: next(workspace_ids),
            source_event_types=("git.baseline_failed",),
        )
        restored = deserialize_project_state(
            serialize_project_state(workspace_block)
        )
        self.assertIs(
            restored.human_actions[-1].category,
            HumanActionCategory.WORKSPACE_BLOCK,
        )

    def test_worker_input_round_trip_and_v8_migration(self):
        identifiers = iter(("source-input", "requested-input"))
        gated = request_human_action(
            make_project_state(),
            category=HumanActionCategory.WORKER_INPUT,
            summary="Worker needs input",
            requested_action="Answer the question",
            risk="Partial changes may exist",
            task_id="task-1",
            operation_time=NOW,
            action_id="action-input",
            event_id_factory=lambda: next(identifiers),
            source_event_types=("task.human_required",),
            worker_input=WorkerInputDetails(
                "item/tool/requestUserInput",
                "request-1",
                "Choose a framework",
                ("Flask", "FastAPI"),
                1,
            ),
        )
        payload = serialize_project_state(gated)
        restored = deserialize_project_state(payload)
        self.assertEqual(restored, gated)
        self.assertEqual(restored.human_actions[-1].worker_input.question, "Choose a framework")

        payload["schema_version"] = 8
        for report in payload["execution_reports"]:
            report["human_action_required"] = report.pop("human_action") is not None
        for action in payload["human_actions"]:
            action.pop("worker_input")
        migrated = deserialize_project_state(payload)
        self.assertIsNone(migrated.human_actions[-1].worker_input)


if __name__ == "__main__":
    unittest.main()
