from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest

from code_mule.runtime_handoff import (
    DeliverableType,
    InvalidDeliveryManifest,
    parse_manifest_candidate,
    validate_manifest,
)
from code_mule.state.serialization import deserialize_project_state, serialize_project_state
from state import make_project_state


NOW = datetime(2026, 9, 12, tzinfo=UTC)


def candidate(*, kind="static_web", runnable=True):
    return {
        "deliverable_type": kind,
        "runnable": runnable,
        "entry_point": "index.html" if kind == "static_web" else "src/pkg.py",
        "launch_spec": ({
            "command": {"executable": "python3", "args": ["-m", "http.server", "{port}", "--bind", "127.0.0.1"]},
            "working_directory": ".",
            "environment_keys": [],
            "startup_timeout_seconds": 3,
            "expected_long_running": True,
            "requires_args": False,
            "supports_dynamic_port": True,
        } if runnable else None),
        "verification_spec": {"required_paths": ["index.html" if kind == "static_web" else "src/pkg.py"], "launch_smoke_test_supported": runnable},
        "health_check_spec": {"type": "http" if runnable else "none", "url": "http://127.0.0.1:{port}/" if runnable else None, "command": None, "expected_status": 200 if runnable else None, "timeout_seconds": 1},
        "access_spec": {"host": "127.0.0.1", "port": None, "path": "/"} if runnable else None,
        "stop_spec": {"grace_seconds": 2} if runnable else None,
        "required_environment": [],
        "runtime_generated_paths": [".code-mule/runtime"],
        "usage": "Run the local application." if runnable else "Import src/pkg.py.",
    }


class DeliveryManifestContractTests(unittest.TestCase):
    def test_all_deliverable_types_are_stable(self):
        self.assertEqual(
            {item.value for item in DeliverableType},
            {"cli", "service", "static_web", "desktop", "executable", "library", "component", "package", "documentation", "unknown"},
        )

    def test_strict_candidate_materializes_revision_identity(self):
        manifest = parse_manifest_candidate(candidate(), project_id="p", revision_number=2, plan_version=3, generated_at=NOW)
        self.assertEqual((manifest.id, manifest.revision_number, manifest.plan_version), ("manifest-r2-3", 2, 3))
        self.assertTrue(manifest.runnable)

    def test_unknown_fields_shell_commands_secrets_and_remote_health_fail_closed(self):
        variants = []
        extra = candidate(); extra["helper"] = "x"; variants.append(extra)
        shell = candidate(); shell["launch_spec"]["command"]["executable"] = "sh"; variants.append(shell)
        secret = candidate(); secret["usage"] = "API_KEY=secret"; variants.append(secret)
        remote = candidate(); remote["health_check_spec"]["url"] = "https://example.com/"; variants.append(remote)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(InvalidDeliveryManifest):
                parse_manifest_candidate(payload, project_id="p", revision_number=1, plan_version=1, generated_at=NOW)

    def test_schema_v14_migrates_without_fabricating_delivery_truth(self):
        payload = serialize_project_state(make_project_state())
        payload["schema_version"] = 14
        payload.pop("delivery_manifest_required")
        payload.pop("delivery_manifests")
        payload.pop("runtime_sessions")
        restored = deserialize_project_state(payload)
        self.assertFalse(restored.delivery_manifest_required)
        self.assertEqual(restored.delivery_manifests, ())
        self.assertEqual(restored.runtime_sessions, ())

    def test_manifest_round_trip_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "index.html").write_text("ok", encoding="utf-8")
            manifest = parse_manifest_candidate(candidate(), project_id="project-1", revision_number=1, plan_version=1, generated_at=NOW)
            state = replace(make_project_state(), delivery_manifest_required=True, delivery_manifests=(manifest,))
            self.assertEqual(deserialize_project_state(serialize_project_state(state)), state)

    def test_entry_point_and_required_paths_must_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = parse_manifest_candidate(candidate(), project_id="p", revision_number=1, plan_version=1, generated_at=NOW)
            with self.assertRaises(InvalidDeliveryManifest):
                validate_manifest(manifest, root)

    def test_invalid_working_directory_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "index.html").write_text("ok")
            payload = candidate(); payload["launch_spec"]["working_directory"] = "missing"
            manifest = parse_manifest_candidate(payload, project_id="p", revision_number=1, plan_version=1, generated_at=NOW)
            with self.assertRaises(InvalidDeliveryManifest):
                validate_manifest(manifest, root)

    def test_structured_argv_is_accepted_and_shell_string_shape_is_rejected(self):
        parse_manifest_candidate(candidate(), project_id="p", revision_number=1, plan_version=1, generated_at=NOW)
        payload = candidate(); payload["launch_spec"]["command"] = "python3 -m http.server"
        with self.assertRaises(InvalidDeliveryManifest):
            parse_manifest_candidate(payload, project_id="p", revision_number=1, plan_version=1, generated_at=NOW)

    def test_non_runnable_library_is_valid(self):
        payload = candidate(kind="library", runnable=False)
        manifest = parse_manifest_candidate(payload, project_id="p", revision_number=1, plan_version=1, generated_at=NOW)
        self.assertFalse(manifest.runnable)
        self.assertIsNone(manifest.launch_spec)

    def test_unknown_deliverable_can_never_be_runnable(self):
        payload = candidate(kind="unknown", runnable=True)
        with self.assertRaises(InvalidDeliveryManifest):
            parse_manifest_candidate(payload, project_id="p", revision_number=1, plan_version=1, generated_at=NOW)

    def test_dynamic_port_requires_explicit_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "index.html").write_text("ok")
            payload = candidate(); payload["launch_spec"]["command"]["args"] = ["-m", "http.server"]
            manifest = parse_manifest_candidate(payload, project_id="p", revision_number=1, plan_version=1, generated_at=NOW)
            with self.assertRaises(InvalidDeliveryManifest):
                validate_manifest(manifest, root)


if __name__ == "__main__":
    unittest.main()
