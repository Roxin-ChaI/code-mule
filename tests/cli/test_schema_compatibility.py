"""Release-facing coverage for CLI/schema and installation compatibility."""

from dataclasses import replace
import hashlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli import CliCommandResult, CliExitCode, main
from code_mule.cli.compatibility import inspect_state_file_schema
from code_mule.installation import (
    diagnose_installation,
    read_install_metadata,
    render_version,
)
from code_mule.onboarding import DoctorService, render_doctor
from code_mule.state import (
    CURRENT_SCHEMA_VERSION,
    JsonProjectStateStore,
    StateSchemaCompatibilityCode,
    StateSchemaCompatibilityError,
    serialize_project_state,
)
from tests.state import make_project_state


ROOT = Path(__file__).resolve().parents[2]


class _Commands:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self, verbose=False):
        self.calls.append("status")
        return CliCommandResult(CliExitCode.SUCCESS, ("ok",))


class StateCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_file = self.root / "project-state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, payload: object) -> bytes:
        self.state_file.write_text(json.dumps(payload), encoding="utf-8")
        return self.state_file.read_bytes()

    def _invoke(self, command: str = "status"):
        commands = _Commands()
        constructed = 0

        def factory(*_):
            nonlocal constructed
            constructed += 1
            return commands

        output, errors = StringIO(), StringIO()
        code = main(
            [command, "--state-file", str(self.state_file)],
            composition_factory=factory,
            environment={"HOME": str(self.root), "PATH": ""},
            stdout=output,
            stderr=errors,
            stdin=StringIO(),
        )
        return code, output.getvalue(), errors.getvalue(), constructed, commands

    def test_newer_state_is_cli_too_old_and_bytes_are_unchanged(self):
        before = self._write({"schema_version": CURRENT_SCHEMA_VERSION + 1})
        digest = hashlib.sha256(before).hexdigest()

        code, _, errors, constructed, _ = self._invoke()

        self.assertEqual(code, CliExitCode.INVALID_PROJECT_STATE)
        self.assertEqual(constructed, 0)
        self.assertIn("CLI UPDATE REQUIRED", errors)
        self.assertIn("CLI_TOO_OLD", errors)
        self.assertIn("Update required", errors)
        self.assertIn("Executable", errors)
        self.assertIn("Build revision", errors)
        self.assertNotIn("PROJECT STATE ERROR", errors)
        self.assertEqual(
            hashlib.sha256(self.state_file.read_bytes()).hexdigest(), digest
        )

    def test_supported_v15_state_migrates_normally(self):
        payload = serialize_project_state(make_project_state())
        payload["schema_version"] = 15
        for attempt in payload["execution_attempts"]:
            attempt.pop("transport", None)
        self._write(payload)

        restored = JsonProjectStateStore(self.state_file).load()

        self.assertEqual(restored.project.id, make_project_state().project.id)

    def test_unsupported_old_schema_is_typed(self):
        self._write({"schema_version": 0})
        with self.assertRaises(StateSchemaCompatibilityError) as raised:
            inspect_state_file_schema(self.state_file)
        self.assertIs(
            raised.exception.compatibility.code,
            StateSchemaCompatibilityCode.STATE_SCHEMA_UNSUPPORTED,
        )

    def test_missing_corrupt_and_invalid_schema_are_distinct(self):
        cases = (
            ({}, StateSchemaCompatibilityCode.STATE_SCHEMA_MISSING),
            ({"schema_version": "16"}, StateSchemaCompatibilityCode.STATE_SCHEMA_INVALID),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                self._write(payload)
                with self.assertRaises(StateSchemaCompatibilityError) as raised:
                    inspect_state_file_schema(self.state_file)
                self.assertIs(raised.exception.compatibility.code, expected)
        self.state_file.write_text("{", encoding="utf-8")
        with self.assertRaises(StateSchemaCompatibilityError) as raised:
            inspect_state_file_schema(self.state_file)
        self.assertIs(
            raised.exception.compatibility.code,
            StateSchemaCompatibilityCode.STATE_DOCUMENT_CORRUPT,
        )

    def test_stateful_cli_and_tui_commands_share_preflight(self):
        before = self._write({"schema_version": CURRENT_SCHEMA_VERSION + 1})
        for command in ("start", "status", "inspect", "recover", "ui"):
            with self.subTest(command=command):
                code, _, errors, constructed, _ = self._invoke(command)
                self.assertEqual(code, CliExitCode.INVALID_PROJECT_STATE)
                self.assertEqual(constructed, 0)
                self.assertIn("CLI_TOO_OLD", errors)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_current_v16_state_reaches_command_without_regression(self):
        self._write(serialize_project_state(make_project_state()))
        code, output, errors, constructed, commands = self._invoke()
        self.assertEqual((code, errors, constructed), (0, "", 1))
        self.assertEqual(output, "ok\n")
        self.assertEqual(commands.calls, ["status"])

    def test_non_tty_output_is_stable_and_generic_error_is_absent(self):
        self._write({"schema_version": CURRENT_SCHEMA_VERSION + 4})
        first = self._invoke()[2]
        second = self._invoke()[2]
        self.assertEqual(first, second)
        self.assertNotIn("PROJECT STATE ERROR", first)

    def test_doctor_reports_typed_schema_without_loading_domain_state(self):
        self._write({"schema_version": CURRENT_SCHEMA_VERSION + 1})
        workspace = self.root / "workspace"
        workspace.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=workspace, check=True)
        subprocess.run(
            ("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"),
            cwd=workspace,
            check=True,
        )
        report = DoctorService(environment={"PATH": "/usr/bin:/bin"}).diagnose(
            workspace, self.state_file
        )
        rendered = "\n".join(render_doctor(report, verbose=True))
        self.assertIn("CLI_TOO_OLD", rendered)
        self.assertIn("Update required", rendered)


class InstallationCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.prefix = self.home / ".local/share/code-mule"
        self.bin_dir = self.home / ".local/bin"
        self.target = self.prefix / "venv/bin/code-mule"
        self.launcher = self.bin_dir / "code-mule"
        self.target.parent.mkdir(parents=True)
        self.bin_dir.mkdir(parents=True)
        self.target.write_text("#!/bin/sh\n", encoding="utf-8")
        self.target.chmod(0o755)
        self.launcher.symlink_to(self.target)

    def tearDown(self):
        self.temporary.cleanup()

    def _revision(self) -> str:
        return subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=ROOT, text=True,
            capture_output=True, check=True,
        ).stdout.strip()

    def _marker(self, revision: str) -> Path:
        marker = self.prefix / "CODE_MULE_INSTALL"
        marker.write_text(
            "\n".join((
                "Code Mule persistent installation",
                "version: 0.2.0",
                f"prefix: {self.prefix}",
                f"source: {ROOT}",
                f"launcher: {self.launcher}",
                f"source_revision: {revision}",
                "schema_min: 1",
                "schema_max: 16",
                "installed_at: 2026-09-19T00:00:00Z",
                "status: ready",
                "",
            )),
            encoding="utf-8",
        )
        return marker

    def _diagnose(self):
        return diagnose_installation(
            environment={"HOME": str(self.home), "PATH": str(self.bin_dir)},
            executable=self.launcher,
            package_root=ROOT / "src/code_mule",
        )

    def test_stale_global_install_is_detected_from_same_source_checkout(self):
        self._marker("0" * 40)
        result = self._diagnose()
        self.assertEqual(result.global_install_status, "STALE GLOBAL INSTALL")
        self.assertEqual(
            result.update_command,
            f"bash {ROOT / 'scripts/install.sh'} --reinstall",
        )

    def test_matching_installed_and_source_revision_is_managed(self):
        metadata = read_install_metadata(self._marker(self._revision()))
        result = self._diagnose()
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.source_revision, self._revision())
        self.assertEqual(result.global_install_status, "MANAGED")

    def test_legacy_marker_from_same_checkout_is_stale_not_invisible(self):
        marker = self._marker(self._revision())
        text = marker.read_text(encoding="utf-8")
        text = "\n".join(
            line for line in text.splitlines()
            if not line.startswith(("source_revision:", "schema_min:", "schema_max:"))
        ) + "\n"
        marker.write_text(text, encoding="utf-8")
        result = self._diagnose()
        self.assertEqual(result.global_install_status, "STALE GLOBAL INSTALL")

    def test_arbitrary_workspace_does_not_report_source_mismatch(self):
        self._marker("0" * 40)
        arbitrary = self.root / "workspace/code_mule"
        arbitrary.mkdir(parents=True)
        result = diagnose_installation(
            environment={"HOME": str(self.home), "PATH": str(self.bin_dir)},
            executable=self.launcher,
            package_root=arbitrary,
        )
        self.assertEqual(result.global_install_status, "MANAGED")

    def test_symlink_launcher_resolves_owned_persistent_install(self):
        self._marker(self._revision())
        result = self._diagnose()
        self.assertEqual(result.resolved_executable, self.target.resolve())
        self.assertIsNotNone(result.install_metadata)

    def test_verbose_version_exposes_bounded_compatibility_facts(self):
        self._marker("0" * 40)
        rendered = "\n".join(render_version(self._diagnose(), verbose=True))
        self.assertIn("Schema support  1..16", rendered)
        self.assertIn("STALE GLOBAL INSTALL", rendered)
        self.assertIn("Update required", rendered)
        self.assertNotIn("DEEPSEEK_API_KEY", rendered)

    def test_marker_reader_rejects_oversized_or_invalid_metadata(self):
        marker = self._marker(self._revision())
        marker.write_text("x" * 9000, encoding="utf-8")
        self.assertIsNone(read_install_metadata(marker))


if __name__ == "__main__":
    unittest.main()
