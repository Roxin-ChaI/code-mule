import copy
import json
import unittest

from code_mule.worker.contracts import CodexWorkerError
from code_mule.worker.report_contract import (
    ReportFailureStage,
    ReportValidationCode,
)
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
                "required": True,
            },
            {"name": "integration", "status": "not_run", "detail": None, "required": False},
        ],
        "static_checks": [
            {"name": "compileall", "status": "unknown", "detail": None, "required": True}
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

    def test_required_is_mandatory_and_strict_boolean(self):
        for value in (None, "false", 0, 1, [], "missing"):
            payload = valid_payload()
            if value == "missing":
                del payload["tests"][0]["required"]
            else:
                payload["tests"][0]["required"] = value
            with self.subTest(value=value), self.assertRaises(InvalidWorkerReport):
                parse_structured_worker_report(json.dumps(payload))
        parsed = parse_structured_worker_report(json.dumps(valid_payload()))
        self.assertTrue(parsed.tests[0].required)
        self.assertFalse(parsed.tests[1].required)

    def test_extractor_accepts_the_envelopes_real_codex_emits(self):
        payload = json.dumps(valid_payload())
        accepted = {
            "bare": payload,
            "json_fence": "```json\n" + payload + "\n```",
            "plain_fence": "```\n" + payload + "\n```",
            "prose_then_fence": (
                "Done. `value.txt` now contains `value = 42`.\n\n"
                "```json\n" + payload + "\n```\n"
            ),
            "prose_then_object": "Execution report: " + payload,
            "padded": "\n\n  " + payload + "  \n\n",
        }
        for name, raw in accepted.items():
            with self.subTest(envelope=name):
                parsed = parse_structured_worker_report(raw)
                self.assertIs(parsed.status, WorkerExecutionStatus.COMPLETED)

    def test_extractor_never_repairs_json_or_guesses_between_candidates(self):
        payload = json.dumps(valid_payload())
        rejected = {
            "truncated": payload[:-1],
            "not_an_object": "[]",
            "two_fences": (
                "```json\n" + payload + "\n```\n```json\n" + payload + "\n```"
            ),
            "two_objects": "First " + payload + " then " + payload,
            "empty": "",
            "prose_only": "I finished the task and updated value.txt.",
            "single_quotes": payload.replace('"', "'"),
        }
        for name, raw in rejected.items():
            with self.subTest(envelope=name):
                with self.assertRaises(InvalidWorkerReport):
                    parse_structured_worker_report(raw)

    def test_report_failure_carries_a_typed_stage_code_and_field_path(self):
        payload = valid_payload()
        del payload["tests"]
        with self.assertRaises(InvalidWorkerReport) as caught:
            parse_structured_worker_report(
                "Here is the report:\n```json\n" + json.dumps(payload) + "\n```"
            )
        error = caught.exception
        self.assertIs(error.stage, ReportFailureStage.SCHEMA)
        self.assertIs(error.code, ReportValidationCode.MISSING_FIELD)
        self.assertEqual(error.field_path, "worker report.tests")
        self.assertTrue(error.candidate_found)
        self.assertTrue(error.json_decoded)
        self.assertTrue(error.semantic_validation_started)

        with self.assertRaises(InvalidWorkerReport) as fence_failure:
            parse_structured_worker_report("```json\n{not json}\n```")
        self.assertIs(fence_failure.exception.stage, ReportFailureStage.JSON_DECODE)
        self.assertIs(
            fence_failure.exception.code, ReportValidationCode.INVALID_JSON
        )

        with self.assertRaises(InvalidWorkerReport) as no_candidate:
            parse_structured_worker_report("nothing structured here")
        self.assertIs(no_candidate.exception.stage, ReportFailureStage.EXTRACTION)
        self.assertIs(
            no_candidate.exception.code, ReportValidationCode.NO_JSON_CANDIDATE
        )
        self.assertFalse(no_candidate.exception.candidate_found)

    def test_invalid_enum_reports_the_exact_field_path(self):
        payload = valid_payload()
        payload["git_state"] = "cleanish"
        with self.assertRaises(InvalidWorkerReport) as caught:
            parse_structured_worker_report(json.dumps(payload))
        self.assertIs(caught.exception.code, ReportValidationCode.INVALID_ENUM)
        self.assertEqual(caught.exception.field_path, "worker report.git_state")

        payload = valid_payload()
        payload["tests"][0]["status"] = "maybe"
        with self.assertRaises(InvalidWorkerReport) as nested:
            parse_structured_worker_report(json.dumps(payload))
        self.assertEqual(nested.exception.field_path, "worker report.tests[0].status")

    def test_invalid_report_is_a_worker_boundary_error(self):
        self.assertTrue(issubclass(InvalidWorkerReport, CodexWorkerError))


class WorkerReportContractMapTests(unittest.TestCase):
    """The prompt, the schema, and the validator must state one contract."""

    def test_prompt_envelope_names_exactly_the_schema_fields(self):
        import re

        from code_mule.worker.report_contract import REPORT_ENVELOPE_INSTRUCTION

        schema_fields = set(structured_worker_report_schema()["required"])
        match = re.search(
            r"top-level fields must be exactly: ([^.]+)\.", REPORT_ENVELOPE_INSTRUCTION
        )
        self.assertIsNotNone(match, REPORT_ENVELOPE_INSTRUCTION)
        named = {item.strip() for item in match.group(1).split(",")}
        self.assertEqual(named, schema_fields)

    def test_schema_and_parser_reject_the_same_envelope_breaches(self):
        schema = structured_worker_report_schema()
        self.assertFalse(schema["additionalProperties"])
        payload = valid_payload()
        payload["unexpected"] = "value"
        with self.assertRaises(InvalidWorkerReport) as extra:
            parse_structured_worker_report(json.dumps(payload))
        self.assertIs(extra.exception.code, ReportValidationCode.EXTRA_FIELD)
        self.assertEqual(extra.exception.field_path, "worker report.unexpected")

    def test_worker_prompt_states_the_envelope_it_must_obey(self):
        from code_mule.worker.report_contract import REPORT_ENVELOPE_INSTRUCTION

        for requirement in (
            "exactly one JSON object",
            "Do not add prose",
            "Do not wrap it in a ```json code fence",
        ):
            self.assertIn(requirement, REPORT_ENVELOPE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
