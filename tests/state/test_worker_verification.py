import copy
import unittest
from dataclasses import replace

from code_mule.domain.worker_verification import WorkerCheckStatus, WorkerCheckType, WorkerVerificationCheck, legacy_checks
from code_mule.state.serialization import deserialize_project_state, serialize_project_state, InvalidProjectState
from state import make_project_state


class VerificationPersistenceTests(unittest.TestCase):
    def test_typed_evidence_round_trips_and_legacy_is_required(self):
        state = make_project_state()
        check = WorkerVerificationCheck("visual", WorkerCheckType.TEST, WorkerCheckStatus.NOT_RUN, False)
        state = replace(state, execution_reports=(replace(state.execution_reports[0], tests=("visual: not_run",), static_checks=(), verification_checks=(check,)),))
        payload = serialize_project_state(state)
        self.assertEqual(payload["schema_version"], 15)
        self.assertEqual(deserialize_project_state(payload), state)
        old = copy.deepcopy(payload)
        old["schema_version"] = 10
        del old["execution_reports"][0]["verification_checks"]
        original = copy.deepcopy(old)
        migrated = deserialize_project_state(old)
        self.assertEqual(old, original)
        report = migrated.execution_reports[0]
        self.assertIsNone(report.verification_checks)
        evidence = legacy_checks(report.tests, report.static_checks)
        self.assertTrue(evidence[0].required)
        self.assertFalse(evidence[0].permits_delivery)

    def test_current_schema_requires_field_and_strict_typed_records(self):
        payload = serialize_project_state(make_project_state())
        report = payload["execution_reports"][0]
        del report["verification_checks"]
        with self.assertRaises(InvalidProjectState):
            deserialize_project_state(payload)
        for required in (None, "false", 1):
            report["verification_checks"] = [{"name": "test", "check_type": "test", "status": "pass", "required": required}]
            with self.subTest(required=required), self.assertRaises(InvalidProjectState):
                deserialize_project_state(payload)
