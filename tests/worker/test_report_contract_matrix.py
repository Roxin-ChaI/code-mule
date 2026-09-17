"""Deterministic matrix and stress coverage for the Worker report contract.

Every accepted case must parse; every rejected case must carry a stable,
typed stage/code/field path.  Nothing here calls a model or the network.
"""

from __future__ import annotations

import json
import unittest

from code_mule.worker.report_contract import (
    MAX_REPORT_MESSAGE_LENGTH,
    ReportExtractionMode,
    ReportFailureStage,
    ReportValidationCode,
    extract_report_candidate,
)
from code_mule.worker.structured_report import (
    InvalidWorkerReport,
    parse_structured_worker_report,
)


def report(**overrides) -> dict:
    payload = {
        "status": "completed",
        "summary": "Deterministic report",
        "files_changed": ["value.txt"],
        "tests": [
            {
                "name": "local verification",
                "status": "pass",
                "detail": None,
                "required": True,
            }
        ],
        "static_checks": [],
        "git_state": "dirty",
        "issues": [],
        "human_action": None,
    }
    payload.update(overrides)
    return payload


def raw(**overrides) -> str:
    return json.dumps(report(**overrides))


ACCEPTED: dict[str, tuple[str, ReportExtractionMode, str]] = {
    "raw_json": (raw(), ReportExtractionMode.WHOLE_MESSAGE, "completed"),
    "json_fence": (
        "```json\n" + raw() + "\n```",
        ReportExtractionMode.CODE_FENCE,
        "completed",
    ),
    "preamble_then_json_fence": (
        "Done. `value.txt` now contains `value = 42`.\n\n"
        "```json\n" + raw() + "\n```",
        ReportExtractionMode.CODE_FENCE,
        "completed",
    ),
    "embedded_json": (
        "Execution report: " + raw(),
        ReportExtractionMode.EMBEDDED_OBJECT,
        "completed",
    ),
    "bare_fence": (
        "```\n" + raw() + "\n```",
        ReportExtractionMode.CODE_FENCE,
        "completed",
    ),
    "padded_whitespace": (
        "\n\n  " + raw() + "  \n",
        ReportExtractionMode.WHOLE_MESSAGE,
        "completed",
    ),
    "unicode_prose_preamble": (
        "任务完成。\n\n```json\n" + raw() + "\n```",
        ReportExtractionMode.CODE_FENCE,
        "completed",
    ),
    "human_action_input": (
        json.dumps(
            report(
                status="blocked",
                human_action={
                    "kind": "input",
                    "summary": "Need a decision",
                    "request": "Which option should I use?",
                    "choices": ["a", "b"],
                },
            )
        ),
        ReportExtractionMode.WHOLE_MESSAGE,
        "blocked",
    ),
}


REJECTED: dict[str, tuple[str, ReportFailureStage, ReportValidationCode, str | None]] = {
    "ambiguous_two_fences": (
        "```json\n" + raw() + "\n```\n```json\n" + raw(status="blocked") + "\n```",
        ReportFailureStage.EXTRACTION,
        ReportValidationCode.AMBIGUOUS_JSON_CANDIDATE,
        "code_fence_blocks",
    ),
    "ambiguous_two_objects": (
        "First " + raw() + " then " + raw(status="blocked"),
        ReportFailureStage.EXTRACTION,
        ReportValidationCode.AMBIGUOUS_JSON_CANDIDATE,
        "embedded_objects",
    ),
    "malformed_json_in_fence": (
        "```json\n" + raw()[:-3] + "\n```",
        ReportFailureStage.JSON_DECODE,
        ReportValidationCode.INVALID_JSON,
        "code_fence_body",
    ),
    "malformed_embedded_object": (
        "Report: {not json at all}",
        ReportFailureStage.JSON_DECODE,
        ReportValidationCode.INVALID_JSON,
        "embedded_object",
    ),
    "prose_only": (
        "I updated value.txt and the verification passed.",
        ReportFailureStage.EXTRACTION,
        ReportValidationCode.NO_JSON_CANDIDATE,
        None,
    ),
    "empty": ("", ReportFailureStage.ENVELOPE, ReportValidationCode.EMPTY_OUTPUT, None),
    "whitespace_only": (
        "   \n\t ",
        ReportFailureStage.ENVELOPE,
        ReportValidationCode.EMPTY_OUTPUT,
        None,
    ),
    "array": (
        "[1, 2, 3]",
        ReportFailureStage.SCHEMA,
        ReportValidationCode.NOT_AN_OBJECT,
        "worker report",
    ),
    "string": (
        '"just a string"',
        ReportFailureStage.SCHEMA,
        ReportValidationCode.NOT_AN_OBJECT,
        "worker report",
    ),
    "scalar": (
        "42",
        ReportFailureStage.SCHEMA,
        ReportValidationCode.NOT_AN_OBJECT,
        "worker report",
    ),
    "missing_field": (
        json.dumps({k: v for k, v in report().items() if k != "tests"}),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.MISSING_FIELD,
        "worker report.tests",
    ),
    "extra_field": (
        json.dumps(report() | {"extra": "value"}),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.EXTRA_FIELD,
        "worker report.extra",
    ),
    "invalid_status_enum": (
        raw(status="finished"),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_ENUM,
        "worker report.status",
    ),
    "invalid_git_state_enum": (
        raw(git_state="cleanish"),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_ENUM,
        "worker report.git_state",
    ),
    "invalid_check_status_enum": (
        json.dumps(
            report(
                tests=[
                    {
                        "name": "check",
                        "status": "maybe",
                        "detail": None,
                        "required": True,
                    }
                ]
            )
        ),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_ENUM,
        "worker report.tests[0].status",
    ),
    "invalid_human_action_kind_enum": (
        json.dumps(
            report(
                human_action={
                    "kind": "maybe",
                    "summary": "s",
                    "request": "r",
                    "choices": [],
                }
            )
        ),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_HUMAN_ACTION,
        "worker report.human_action.kind",
    ),
    "summary_not_a_string": (
        raw(summary=["array"]),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_FIELD_TYPE,
        "worker report.summary",
    ),
    "files_changed_not_an_array": (
        raw(files_changed="value.txt"),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_FIELD_TYPE,
        "worker report.files_changed",
    ),
    "check_required_not_boolean": (
        json.dumps(
            report(
                tests=[
                    {
                        "name": "check",
                        "status": "pass",
                        "detail": None,
                        "required": "true",
                    }
                ]
            )
        ),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_CHECK_RESULT,
        "worker report.tests[0].required",
    ),
    "check_detail_not_string_or_null": (
        json.dumps(
            report(
                tests=[
                    {
                        "name": "check",
                        "status": "pass",
                        "detail": 7,
                        "required": True,
                    }
                ]
            )
        ),
        ReportFailureStage.SCHEMA,
        ReportValidationCode.INVALID_FIELD_TYPE,
        "worker report.tests[0].detail",
    ),
    "human_action_semantic_conflict": (
        json.dumps(
            report(
                human_action={
                    "kind": "approval",
                    "summary": "approve",
                    "request": "please approve",
                    "choices": ["only-valid-for-input"],
                }
            )
        ),
        ReportFailureStage.SEMANTIC_VALIDATION,
        ReportValidationCode.INVALID_HUMAN_ACTION,
        "worker report.human_action",
    ),
}


class ReportCaseMatrixTests(unittest.TestCase):
    def test_every_accepted_case_parses_with_the_expected_extraction_mode(self):
        for name, (text, mode, expected_status) in ACCEPTED.items():
            with self.subTest(case=name):
                parsed = parse_structured_worker_report(text)
                self.assertEqual(parsed.status.value, expected_status)
                self.assertIs(extract_report_candidate(text).mode, mode)

    def test_every_rejected_case_has_a_stable_stage_code_and_field(self):
        for name, (text, stage, code, field_path) in REJECTED.items():
            with self.subTest(case=name):
                with self.assertRaises(InvalidWorkerReport) as caught:
                    parse_structured_worker_report(text)
                error = caught.exception
                self.assertIs(error.stage, stage)
                self.assertIs(error.code, code)
                self.assertEqual(error.field_path, field_path)

    def test_oversized_message_is_rejected_without_parsing_it(self):
        huge = "x" * (MAX_REPORT_MESSAGE_LENGTH + 1)
        with self.assertRaises(InvalidWorkerReport) as caught:
            parse_structured_worker_report(huge)
        self.assertIs(caught.exception.stage, ReportFailureStage.ENVELOPE)
        self.assertIs(
            caught.exception.code, ReportValidationCode.OUTPUT_TOO_LARGE
        )

    def test_non_string_output_is_rejected(self):
        for value in (None, 5, b"{}", ["{}"], {"status": "completed"}):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidWorkerReport) as caught:
                    parse_structured_worker_report(value)
                self.assertIs(
                    caught.exception.stage, ReportFailureStage.ENVELOPE
                )
                self.assertIs(
                    caught.exception.code, ReportValidationCode.NOT_A_STRING
                )

    def test_braces_inside_strings_do_not_confuse_extraction(self):
        payload = report(summary="uses { and } characters")
        parsed = parse_structured_worker_report(
            "Here it is:\n```json\n" + json.dumps(payload) + "\n```"
        )
        self.assertEqual(parsed.summary, "uses { and } characters")


class ParserStressTests(unittest.TestCase):
    """≥1000 deterministic iterations: no crash, no accidental accept."""

    ITERATIONS = 1_000

    def test_stress_loop_is_deterministic_and_never_crashes(self):
        accepted_cases = list(ACCEPTED.items())
        rejected_cases = list(REJECTED.items())
        accepts = 0
        rejects = 0
        for index in range(self.ITERATIONS):
            name, (text, _mode, _status) = accepted_cases[index % len(accepted_cases)]
            try:
                parse_structured_worker_report(text)
            except Exception as error:  # noqa: BLE001 - stress must not crash
                self.fail(f"valid case {name} was rejected: {error!r}")
            accepts += 1

            name, (text, stage, code, field_path) = rejected_cases[
                index % len(rejected_cases)
            ]
            try:
                parse_structured_worker_report(text)
            except InvalidWorkerReport as error:
                self.assertIs(error.stage, stage, name)
                self.assertIs(error.code, code, name)
                self.assertEqual(error.field_path, field_path, name)
            except Exception as error:  # noqa: BLE001 - must stay typed
                self.fail(f"invalid case {name} raised {type(error).__name__}: {error!r}")
            else:
                self.fail(f"invalid case {name} was accepted")
            rejects += 1
        self.assertEqual(accepts, self.ITERATIONS)
        self.assertEqual(rejects, self.ITERATIONS)

    def test_repeated_parses_return_the_same_result(self):
        text = (
            "Done.\n\n```json\n" + raw() + "\n```"
        )
        first = parse_structured_worker_report(text)
        for _ in range(200):
            again = parse_structured_worker_report(text)
            self.assertEqual(again, first)


if __name__ == "__main__":
    unittest.main()
