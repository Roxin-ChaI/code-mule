"""Typed delivery-manifest handoff diagnostics and prompt contract coverage.

The v4 failure was a present-but-rejected candidate that reported as
"missing or invalid"; these tests keep every stop in the chain named.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from code_mule.runtime_handoff.contracts import InvalidDeliveryManifest
from code_mule.runtime_handoff.handoff import (
    ManifestFailureCode,
    ManifestFailureStage,
    ManifestHandoffError,
    as_handoff_error,
    manifest_candidate_contract,
    manifest_candidate_prompt,
    manifest_failure_metadata,
)
from code_mule.runtime_handoff.validation import (
    load_and_validate_manifest,
    parse_manifest_candidate,
    validate_manifest,
)
from code_mule.runtime_handoff.service import RuntimeSmokeVerifier

from .test_contracts import candidate
from .test_smoke import FakeProcess


NOW = datetime(2026, 9, 12, tzinfo=UTC)


def write(root: Path, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (root / "code-mule-delivery.json").write_text(text, encoding="utf-8")


def load(root: Path):
    return load_and_validate_manifest(
        root,
        project_id="project-1",
        revision_number=1,
        plan_version=1,
        generated_at=NOW,
    )


class ManifestHandoffChainTests(unittest.TestCase):
    def assert_failure(self, root: Path, stage, code, field_path=None):
        with self.assertRaises(InvalidDeliveryManifest) as caught:
            load(root)
        failure = as_handoff_error(caught.exception)
        self.assertIs(failure.stage, stage)
        self.assertIs(failure.code, code)
        self.assertEqual(failure.field_path, field_path)
        return failure

    def test_valid_candidate_reaches_verified_final_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("ok", encoding="utf-8")
            write(root, candidate())
            manifest = load(root)
            self.assertEqual(manifest.status.value, "verified")
            validate_manifest(manifest, root)

    def test_missing_file_is_named_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = self.assert_failure(
                root,
                ManifestFailureStage.CANDIDATE_LOOKUP,
                ManifestFailureCode.MANIFEST_MISSING,
            )
            self.assertFalse(failure.candidate_present)
            self.assertIn("not found", failure.message.lower())
            metadata = manifest_failure_metadata(failure)
            self.assertEqual(metadata["manifest_candidate_present"], "false")
            self.assertEqual(
                metadata["manifest_candidate_path"], "code-mule-delivery.json"
            )

    def test_invalid_json_is_a_parse_failure_not_a_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, "{not json")
            failure = self.assert_failure(
                root,
                ManifestFailureStage.JSON_DECODE,
                ManifestFailureCode.MANIFEST_PARSE_FAILED,
                field_path="line 1 column 2",
            )
            self.assertTrue(failure.candidate_present)
            self.assertIn("line 1 column", failure.field_path)
            self.assertNotIn("missing", failure.message.lower())

    def test_schema_invalid_candidate_names_the_exact_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = candidate()
            payload["deliverable_type"] = "http-service"
            write(root, payload)
            failure = self.assert_failure(
                root,
                ManifestFailureStage.CONTRACT_PARSE,
                ManifestFailureCode.MANIFEST_VALIDATION_FAILED,
                field_path="deliverable_type",
            )
            self.assertTrue(failure.candidate_present)
            self.assertIn("deliverable_type", failure.requested_action)

    def test_extra_and_missing_fields_fail_closed_with_a_field_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = candidate()
            payload["helper"] = "x"
            write(root, payload)
            self.assert_failure(
                root,
                ManifestFailureStage.CONTRACT_PARSE,
                ManifestFailureCode.MANIFEST_VALIDATION_FAILED,
                field_path="helper",
            )

    def test_wrong_handoff_path_reads_the_repository_root_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs" / "code-mule-delivery.json").write_text(
                json.dumps(candidate()), encoding="utf-8"
            )
            failure = self.assert_failure(
                root,
                ManifestFailureStage.CANDIDATE_LOOKUP,
                ManifestFailureCode.MANIFEST_MISSING,
            )
            self.assertEqual(failure.candidate_path, "code-mule-delivery.json")

    def test_present_but_invalid_candidate_never_reads_as_missing(self):
        """The exact v4 shape: correct top-level keys, invented nested dialect."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root,
                {
                    "deliverable_type": "http-service",
                    "runnable": True,
                    "entry_point": "server.py",
                    "launch_spec": {
                        "executable": "python3",
                        "args": ["server.py", "--host", "127.0.0.1", "--port", "{port}"],
                        "working_directory": "repository_root",
                        "port_strategy": "dynamic",
                    },
                    "verification_spec": {"sandbox_safe": True},
                    "health_check_spec": {"kind": "http", "path": "/health"},
                    "access_spec": {"host": "127.0.0.1", "port": None, "path": "/"},
                    "stop_spec": {"grace_seconds": 5},
                    "required_environment": [],
                    "runtime_generated_paths": [".code-mule/runtime"],
                    "usage": "Run code-mule launch.",
                },
            )
            failure = self.assert_failure(
                root,
                ManifestFailureStage.CONTRACT_PARSE,
                ManifestFailureCode.MANIFEST_VALIDATION_FAILED,
                field_path="deliverable_type",
            )
            self.assertNotIn("missing or invalid", failure.message.lower())
            self.assertTrue(failure.candidate_present)

    def test_every_failure_carries_a_bounded_safe_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, json.dumps(candidate()) + "\n{{")
            with self.assertRaises(InvalidDeliveryManifest) as caught:
                load(root)
            failure = as_handoff_error(caught.exception)
            metadata = manifest_failure_metadata(failure)
            self.assertLessEqual(len(metadata["manifest_summary"]), 300)
            self.assertIn(
                metadata["manifest_code"],
                {item.value for item in ManifestFailureCode},
            )

    def test_untyped_manifest_errors_still_gain_the_typed_vocabulary(self):
        failure = as_handoff_error(InvalidDeliveryManifest("something else"))
        self.assertIs(failure.code, ManifestFailureCode.MANIFEST_HANDOFF_FAILED)
        self.assertEqual(
            manifest_failure_metadata(failure)["manifest_code"],
            "manifest_handoff_failed",
        )


class ManifestPromptContractTests(unittest.TestCase):
    """The prompt skeleton must satisfy the validator it is checked against."""

    def test_service_manifest_passes_the_real_bounded_runtime_smoke(self):
        contract = manifest_candidate_contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.py").write_text("x", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_server.py").write_text("x", encoding="utf-8")
            payload = json.loads(json.dumps(contract.service_example))
            payload["runtime_generated_paths"] = []
            manifest = parse_manifest_candidate(
                payload,
                project_id="p",
                revision_number=1,
                plan_version=1,
                generated_at=NOW,
            )
            process = FakeProcess()
            urls: list[str] = []
            verifier = RuntimeSmokeVerifier(
                environment={"PATH": "/usr/bin:/bin"},
                popen=lambda argv, **kwargs: process,
                http_status=lambda url, timeout: urls.append(url) or 200,
                dynamic_port_factory=lambda: 41511,
                port_available=lambda port: True,
                sleeper=lambda seconds: None,
            )
            verifier.verify(manifest, root)
            self.assertTrue(process.terminated)
            self.assertEqual(urls, ["http://127.0.0.1:41511/health"])

    def test_service_skeleton_validates_against_the_real_validator(self):
        contract = manifest_candidate_contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.py").write_text("x", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_server.py").write_text("x", encoding="utf-8")
            manifest = parse_manifest_candidate(
                contract.service_example,
                project_id="p",
                revision_number=1,
                plan_version=1,
                generated_at=NOW,
            )
            validate_manifest(manifest, root)
            self.assertTrue(manifest.launch_spec.supports_dynamic_port)

    def test_non_runnable_skeleton_parses(self):
        contract = manifest_candidate_contract()
        manifest = parse_manifest_candidate(
            contract.non_runnable_example,
            project_id="p",
            revision_number=1,
            plan_version=1,
            generated_at=NOW,
        )
        self.assertFalse(manifest.runnable)

    def test_prompt_states_the_exact_contract_and_the_verification_boundary(self):
        prompt = manifest_candidate_prompt()
        contract = manifest_candidate_contract()
        for field in contract.top_level_fields:
            self.assertIn(field, prompt)
        self.assertIn("deliverable_type must be one of:", prompt)
        self.assertIn("Do not invent extra fields", prompt)
        self.assertIn("Final Verification alone performs launch", prompt)


if __name__ == "__main__":
    unittest.main()
