import copy
import json
import unittest

from code_mule.worker.contracts import CodexWorkerError
from code_mule.worker.structured_report import (
    InvalidWorkerReport,
    WorkerCheckStatus,
    WorkerExecutionStatus,
    parse_structured_worker_report,
    structured_worker_report_schema,
)
from code_mule.domain.enums import WorkerHumanActionKind


def valid_payload():
    return {
        "status": "completed",
        "summary": "Fixed addition",
        "files_changed": ["calculator.py", "test_calculator.py"],
        "tests": [
            {
                "name": "python -m unittest",
                "status": "pass",
                "detail": "1 test passed",
            },
            {"name": "integration", "status": "not_run", "detail": None},
        ],
        "static_checks": [
            {"name": "compileall", "status": "unknown", "detail": None}
        ],
        "git_state": "dirty",
        "issues": ["issue-b", "issue-a"],
        "human_action": None,
    }


class StructuredWorkerReportTests(unittest.TestCase):
    def test_schema_is_strict_recursive_and_fresh(self):
        first = structured_worker_report_schema()
        second = structured_worker_report_schema()
        self.assertIsNot(first, second)
        self.assertFalse(first["additionalProperties"])
        self.assertEqual(set(first["required"]), set(first["properties"]))
        for field in ("tests", "static_checks"):
            nested = first["properties"][field]["items"]
            self.assertFalse(nested["additionalProperties"])
            self.assertEqual(set(nested["required"]), set(nested["properties"]))
        action = first["properties"]["human_action"]["anyOf"][1]
        self.assertFalse(action["additionalProperties"])
        self.assertEqual(set(action["required"]), set(action["properties"]))
        self.assertNotIn("human_action_required", first["properties"])
        first["changed"] = True
        self.assertNotIn("changed", second)

    def test_parser_returns_typed_report_and_preserves_array_order(self):
        report = parse_structured_worker_report(json.dumps(valid_payload()))
        self.assertIs(report.status, WorkerExecutionStatus.COMPLETED)
        self.assertEqual(
            report.files_changed, ("calculator.py", "test_calculator.py")
        )
        self.assertIs(report.tests[0].status, WorkerCheckStatus.PASS)
        self.assertIs(report.tests[1].status, WorkerCheckStatus.NOT_RUN)
        self.assertEqual(report.issues, ("issue-b", "issue-a"))
        self.assertFalse(report.human_action_required)

    def test_typed_human_action_preserves_kind_request_and_choices(self):
        payload = valid_payload()
        payload["human_action"] = {
            "kind": "input",
            "summary": "Boss choice required",
            "request": "Use localStorage or session memory?",
            "choices": ["localStorage", "session memory"],
        }

        report = parse_structured_worker_report(json.dumps(payload))

        self.assertIs(report.human_action.kind, WorkerHumanActionKind.INPUT)
        self.assertEqual(report.human_action.choices, ("localStorage", "session memory"))
        self.assertTrue(report.human_action_required)

    def test_human_action_is_strict_bounded_and_not_inferred_from_summary(self):
        cases = []
        for action in (
            {"kind": "input", "summary": "x", "request": "q", "choices": [], "extra": "x"},
            {"kind": "unknown", "summary": "x", "request": "q", "choices": []},
            {"kind": "approval", "summary": "x", "request": "q", "choices": ["yes"]},
            {"kind": "input", "summary": "x" * 1001, "request": "q", "choices": []},
            {"kind": "input", "summary": "x", "request": "q", "choices": ["x"] * 21},
        ):
            payload = valid_payload()
            payload["human_action"] = action
            cases.append(payload)
        for payload in cases:
            with self.subTest(action=payload["human_action"]):
                with self.assertRaises(InvalidWorkerReport):
                    parse_structured_worker_report(json.dumps(payload))

    def test_missing_extra_wrong_type_and_invalid_enums_fail_closed(self):
        cases = []
        missing = valid_payload()
        missing.pop("git_state")
        cases.append(missing)
        extra = valid_payload()
        extra["extra"] = True
        cases.append(extra)
        wrong_type = valid_payload()
        wrong_type["files_changed"] = "calculator.py"
        cases.append(wrong_type)
        bad_status = valid_payload()
        bad_status["status"] = "done"
        cases.append(bad_status)
        bad_git = valid_payload()
        bad_git["git_state"] = "probably clean"
        cases.append(bad_git)
        bad_action = valid_payload()
        bad_action["human_action"] = True
        cases.append(bad_action)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidWorkerReport):
                    parse_structured_worker_report(json.dumps(payload))

    def test_malformed_nested_checks_fail_closed_without_defaults(self):
        cases = []
        for mutation in ("missing", "extra", "empty", "bad_status", "bad_detail"):
            payload = copy.deepcopy(valid_payload())
            check = payload["tests"][0]
            if mutation == "missing":
                check.pop("detail")
            elif mutation == "extra":
                check["result"] = "pass"
            elif mutation == "empty":
                check["name"] = ""
            elif mutation == "bad_status":
                check["status"] = "skipped"
            else:
                check["detail"] = 10
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidWorkerReport):
                    parse_structured_worker_report(json.dumps(payload))

    def test_parser_does_not_extract_or_repair_json(self):
        payload = json.dumps(valid_payload())
        invalid = (
            "```json\n" + payload + "\n```",
            "Execution report: " + payload,
            payload[:-1],
            "[]",
        )
        for raw in invalid:
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(InvalidWorkerReport):
                    parse_structured_worker_report(raw)

    def test_invalid_report_is_a_worker_boundary_error(self):
        self.assertTrue(issubclass(InvalidWorkerReport, CodexWorkerError))


if __name__ == "__main__":
    unittest.main()
