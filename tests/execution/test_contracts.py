from dataclasses import replace
from datetime import UTC, datetime
import copy
import unittest

from code_mule.execution import (
    ExecutionAlreadyOwned,
    ExecutionLease,
    ExecutionLeaseStatus,
    ExecutionRecoveryRequired,
    RecoveryClassification,
    RecoveryDecision,
)
from code_mule.state import (
    CURRENT_SCHEMA_VERSION,
    deserialize_project_state,
    serialize_project_state,
)
from state import make_project_state


NOW = datetime(2026, 9, 3, tzinfo=UTC)


def lease(**changes):
    value = ExecutionLease(
        "lease-1",
        "project-1",
        "owner-1",
        1234,
        NOW,
        NOW,
        ExecutionLeaseStatus.ACTIVE,
        "task-1",
        "thread-1",
        2,
    )
    return replace(value, **changes)


class ExecutionOwnershipContractTests(unittest.TestCase):
    def test_lease_and_recovery_values_are_typed(self):
        value = lease()
        decision = RecoveryDecision(
            RecoveryClassification.SESSION_RECOVERY_REQUIRED,
            value.id,
            value.current_task_id,
        )
        self.assertIs(value.status, ExecutionLeaseStatus.ACTIVE)
        self.assertEqual(value.attempt, 2)
        self.assertIs(
            ExecutionAlreadyOwned(value).lease,
            value,
        )
        self.assertIs(ExecutionRecoveryRequired(decision).decision, decision)

    def test_invalid_identity_and_session_combinations_fail_closed(self):
        with self.assertRaises(ValueError):
            lease(owner_id="")
        with self.assertRaises(ValueError):
            lease(pid=0)
        with self.assertRaises(ValueError):
            lease(current_task_id=None)
        with self.assertRaises(ValueError):
            lease(attempt=0)

    def test_v6_round_trip_preserves_execution_identity(self):
        state = replace(make_project_state(), execution_leases=(lease(),))
        payload = serialize_project_state(state)
        self.assertEqual(payload["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(payload["execution_leases"][0]["owner_id"], "owner-1")
        self.assertEqual(payload["execution_leases"][0]["attempt"], 2)
        self.assertEqual(deserialize_project_state(payload), state)

    def test_v1_through_v5_migrate_to_empty_execution_history(self):
        current = serialize_project_state(make_project_state())
        legacy = copy.deepcopy(current)
        legacy["schema_version"] = 5
        for report in legacy["execution_reports"]:
            report["human_action_required"] = report.pop("human_action") is not None
        legacy.pop("execution_leases")
        migrated = deserialize_project_state(legacy)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 12)
        self.assertEqual(migrated.execution_leases, ())


if __name__ == "__main__":
    unittest.main()
