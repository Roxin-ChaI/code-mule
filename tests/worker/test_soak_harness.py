"""Deterministic self-check for the manual real Worker soak harness.

The real harness must only ever be run by hand against real Codex.  These
tests exercise its classification, verification, and accounting logic against
the deterministic fake app-server so the harness cannot silently mis-report.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from code_mule.transport import TransportFailureKind, WorkerFailureClass
from code_mule.worker import (
    CodexAppServerStartError,
    CodexRequestRejected,
    CodexTurnHardTimeout,
    CodexTurnInactivityTimeout,
    CodexWorkerError,
    InvalidWorkerReport,
)
from code_mule.domain.models import ExecutionReport

_ROOT = Path(__file__).resolve().parents[2]
_SOAK = _ROOT / "scripts" / "real_worker_soak.py"
_FAKE = Path(__file__).with_name("fake_codex_app_server.py")


def load_soak():
    cached = sys.modules.get("real_worker_soak")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("real_worker_soak", _SOAK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolves the defining module through sys.modules.
    sys.modules["real_worker_soak"] = module
    spec.loader.exec_module(module)
    return module


class SoakHarnessUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.soak = load_soak()

    def test_every_boundary_error_maps_to_a_named_bucket(self) -> None:
        cases = (
            (CodexTurnHardTimeout("x"), "timeout"),
            (CodexTurnInactivityTimeout("x"), "timeout"),
            (CodexRequestRejected("x"), "codex_turn_failure"),
            # A rejected structured report is Code Mule's own report-contract
            # boundary, so it must never inflate the transport failure count.
            (InvalidWorkerReport("x"), "runtime_failure"),
            (CodexAppServerStartError("x"), "process_failure"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                bucket, kind, failure_class = self.soak._classify(error)
                self.assertEqual(bucket, expected)
                self.assertIsNotNone(failure_class)
                self.assertIsNotNone(kind)

    def test_untyped_worker_error_is_reported_as_unknown(self) -> None:
        class Unclassified(CodexWorkerError):
            pass

        bucket, kind, failure_class = self.soak._classify(Unclassified("x"))
        self.assertEqual(bucket, "unknown_failure")
        self.assertIsNone(kind)
        self.assertIsNone(failure_class)

    def test_no_typed_transport_kind_is_ever_bucketed_as_unknown(self) -> None:
        for kind in TransportFailureKind:
            with self.subTest(kind=kind.value):
                error = CodexWorkerError("typed")
                error.transport_failure_kind = kind
                bucket, reported_kind, failure_class = self.soak._classify(error)
                self.assertNotEqual(bucket, "unknown_failure")
                self.assertEqual(reported_kind, kind.value)
                self.assertIn(
                    WorkerFailureClass(failure_class),
                    set(WorkerFailureClass),
                )

    def test_report_parse_failure_is_a_runtime_bucket_not_a_transport_bucket(self) -> None:
        for kind in (
            TransportFailureKind.REPORT_PARSE_FAILED,
            TransportFailureKind.REPORT_PERSIST_FAILED,
        ):
            with self.subTest(kind=kind.value):
                bucket, reported_kind, failure_class = self.soak._classify(
                    InvalidWorkerReport("rejected")
                    if kind is TransportFailureKind.REPORT_PARSE_FAILED
                    else self._persist_error(kind)
                )
                self.assertEqual(bucket, "runtime_failure")
                self.assertEqual(failure_class, "code_mule_runtime_failure")
                if kind is TransportFailureKind.REPORT_PARSE_FAILED:
                    self.assertEqual(reported_kind, "report_parse_failed")

    def test_report_contract_bucketing_splits_extraction_and_validation(self) -> None:
        cases = {
            ("envelope", None): "report_extraction",
            ("extraction", None): "report_extraction",
            ("json_decode", None): "report_extraction",
            ("schema", None): "report_validation",
            ("semantic_validation", None): "report_validation",
            (None, "report_parse_failed"): "report_unstaged",
            (None, None): None,
            ("schema", "turn_failed"): "report_validation",
        }
        for (stage, kind), expected in cases.items():
            with self.subTest(stage=stage, kind=kind):
                self.assertEqual(
                    self.soak.report_contract_bucket(stage, kind), expected
                )

    def test_first_real_soak_classifies_without_fabricating_a_stage(self) -> None:
        """The first soak's artifacts predate typed stages; stay honest."""

        # Exactly the shape persisted by artifacts/iteration-02.json.
        recorded = [
            {"failure_kind": "report_parse_failed", "report_stage": None},
            {"failure_kind": "report_parse_failed", "report_stage": None},
        ]
        buckets = [
            self.soak.report_contract_bucket(
                item["report_stage"], item["failure_kind"]
            )
            for item in recorded
        ]
        self.assertEqual(buckets, ["report_unstaged", "report_unstaged"])
        self.assertEqual(
            sum(b == "report_extraction" for b in buckets), 0
        )

    def test_same_envelope_is_extraction_once_the_stage_is_recorded(self) -> None:
        """With v16 diagnostics the same failure is provably an extraction one."""

        self.assertEqual(
            self.soak.report_contract_bucket("json_decode", "report_parse_failed"),
            "report_extraction",
        )

    @staticmethod
    def _persist_error(kind: TransportFailureKind) -> CodexWorkerError:
        error = CodexWorkerError("typed")
        error.transport_failure_kind = kind
        return error

    def test_report_verification_requires_an_edit_a_command_and_a_report(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "value.txt").write_text("value = 42\n", encoding="utf-8")
            good = ExecutionReport(
                "r1",
                "TASK-SOAK-001",
                1,
                "completed",
                ("value.txt",),
                ("python verify: pass",),
                (),
                "dirty",
                (),
                None,
                "ok",
                datetime.now(UTC),
            )
            self.assertIsNone(self.soak._verify_report(good, workspace))

            (workspace / "value.txt").write_text("value = 1\n", encoding="utf-8")
            self.assertIsNotNone(self.soak._verify_report(good, workspace))
            (workspace / "value.txt").write_text("value = 42\n", encoding="utf-8")
            for changes in (
                {"files_changed": ()},
                {"tests": ()},
                {"status": "blocked"},
            ):
                with self.subTest(changes=list(changes)):
                    self.assertIsNotNone(
                        self.soak._verify_report(
                            replace(good, **changes), workspace
                        )
                    )


class SoakHarnessEndToEndTests(unittest.TestCase):
    """Run the real harness end to end against the deterministic fake server."""

    def _shim(self, root: Path, scenario: str) -> dict[str, str]:
        shim_dir = root / "bin"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim = shim_dir / "codex"
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{_FAKE}" {scenario}\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        environment = dict(os.environ)
        environment["PATH"] = f"{shim_dir}{os.pathsep}{environment['PATH']}"
        return environment

    def _run(self, root: Path, scenario: str, *extra: str):
        soak = load_soak()
        artifacts = root / f"artifacts-{scenario}"
        environment = self._shim(root, scenario)
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=False):
            with redirect_stdout(buffer):
                code = soak.main(
                    [
                        "--iterations",
                        "1",
                        "--artifacts-dir",
                        str(artifacts),
                        *extra,
                    ]
                )
        return code, buffer.getvalue(), artifacts

    def _workspaces(self, root: Path) -> list[Path]:
        return sorted(root.glob("**/iteration-*/workspace"))

    def test_failing_iteration_preserves_its_workspace_and_artifact(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "tempfile.mkdtemp", return_value=str(root / "run")
            ):
                (root / "run").mkdir(parents=True, exist_ok=True)
                code, output, artifacts = self._run(root, "turn_failed")
            self.assertEqual(code, 1, output)
            self.assertEqual(
                len(list(artifacts.glob("iteration-*.json"))), 1
            )
            workspaces = self._workspaces(root)
            self.assertEqual(len(workspaces), 1, "failure evidence was deleted")
            self.assertTrue((workspaces[0] / "value.txt").exists())

    def test_passing_iteration_removes_only_its_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("tempfile.mkdtemp", return_value=str(root / "run")):
                (root / "run").mkdir(parents=True, exist_ok=True)
                code, output, artifacts = self._run(root, "soak_edit")
            self.assertEqual(code, 0, output)
            self.assertEqual(self._workspaces(root), [])
            self.assertEqual(len(list(artifacts.glob("iteration-*.json"))), 1)

    def test_purge_failures_is_an_explicit_opt_in_that_keeps_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("tempfile.mkdtemp", return_value=str(root / "run")):
                (root / "run").mkdir(parents=True, exist_ok=True)
                code, output, artifacts = self._run(
                    root, "turn_failed", "--purge-failures"
                )
            self.assertEqual(code, 1, output)
            self.assertEqual(self._workspaces(root), [])
            self.assertEqual(len(list(artifacts.glob("iteration-*.json"))), 1)

    def test_interruption_preserves_every_workspace_and_artifact(self) -> None:
        soak = load_soak()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = root / "artifacts"
            environment = self._shim(root, "soak_edit")
            real_run = soak.run_iteration
            calls = {"count": 0}

            def interrupted(index, run_root, artifacts_dir):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise KeyboardInterrupt
                return real_run(index, run_root, artifacts_dir)

            buffer = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False):
                with mock.patch.object(soak, "run_iteration", interrupted):
                    with mock.patch(
                        "tempfile.mkdtemp", return_value=str(root / "run")
                    ):
                        (root / "run").mkdir(parents=True, exist_ok=True)
                        with redirect_stdout(buffer):
                            code = soak.main(
                                [
                                    "--iterations",
                                    "3",
                                    "--artifacts-dir",
                                    str(artifacts),
                                ]
                            )
            output = buffer.getvalue()
            self.assertEqual(code, 130, output)
            self.assertIn("SOAK INTERRUPTED", output)
            # The interrupted iteration kept its workspace, and the artifact
            # for the completed iteration was not removed.
            self.assertEqual(self._workspaces(root), [])
            self.assertEqual(
                sorted(p.name for p in root.glob("run/iteration-*")),
                ["iteration-01", "iteration-02"],
            )
            self.assertEqual(len(list(artifacts.glob("iteration-*.json"))), 1)

    def test_aggregation_buckets_each_failure_mode_correctly(self) -> None:
        cases = {
            "turn_failed": (
                (),
                ("Codex turn failures  1", "Transport failures   0"),
            ),
            "stdout_eof_exit_nonzero": (
                (),
                ("Process failures     1", "Transport failures   0"),
            ),
            "malformed_jsonrpc": (
                (),
                ("Transport failures   1", "Unknown failures     0"),
            ),
            "inactivity_timeout": (
                ("--inactivity-timeout", "1", "--max-turn-seconds", "2"),
                ("Timeouts             1", "Unknown failures     0"),
            ),
            "prose_only_report": (
                (),
                (
                    "Runtime failures     1",
                    "Report extraction failures  1",
                    "Report validation failures  0",
                    "Transport failures   0",
                    "Unknown failures     0",
                ),
            ),
        }
        for scenario, (extra, expected_lines) in cases.items():
            with self.subTest(scenario=scenario), TemporaryDirectory() as tmp:
                root = Path(tmp)
                with mock.patch(
                    "tempfile.mkdtemp", return_value=str(root / "run")
                ):
                    (root / "run").mkdir(parents=True, exist_ok=True)
                    _code, output, _artifacts = self._run(root, scenario, *extra)
                for expected in expected_lines:
                    self.assertIn(expected, output, output)

    def test_schema_invalid_report_is_counted_as_report_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("tempfile.mkdtemp", return_value=str(root / "run")):
                (root / "run").mkdir(parents=True, exist_ok=True)
                _code, output, _artifacts = self._run(root, "schema_invalid_report")
        self.assertIn("Runtime failures     1", output)
        self.assertIn("Report extraction failures  0", output)
        self.assertIn("Report validation failures  1", output)
        self.assertIn("Transport failures   0", output)
        self.assertIn("Unknown failures     0", output)

    def test_harness_reaches_pass_without_any_real_codex_turn(self) -> None:
        soak = load_soak()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim_dir = root / "bin"
            shim_dir.mkdir()
            shim = shim_dir / "codex"
            shim.write_text(
                "#!/bin/sh\n"
                f'exec "{sys.executable}" "{_FAKE}" soak_edit\n',
                encoding="utf-8",
            )
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
            artifacts = root / "artifacts"
            environment = dict(os.environ)
            environment["PATH"] = f"{shim_dir}{os.pathsep}{environment['PATH']}"
            buffer = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False):
                with redirect_stdout(buffer):
                    code = soak.main(
                        [
                            "--iterations",
                            "2",
                            "--artifacts-dir",
                            str(artifacts),
                        ]
                    )
            output = buffer.getvalue()
            self.assertEqual(code, 0, output)
            self.assertIn("P0 CODEX WORKER RELIABILITY PASS", output)
            self.assertIn("Unknown failures     0", output)
            self.assertEqual(len(list(artifacts.glob("iteration-*.json"))), 2)
            for record in artifacts.glob("iteration-*.json"):
                payload = json.loads(record.read_text(encoding="utf-8"))
                self.assertTrue(payload["passed"])
                self.assertEqual(payload["bucket"], "passed")
                self.assertEqual(payload["transport"]["failure_class"], None)
                self.assertTrue(payload["transport"]["terminal_event_received"])
                self.assertIsNotNone(payload["transport"]["app_server_exit_code"])

    def test_harness_reports_failures_instead_of_faking_success(self) -> None:
        soak = load_soak()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim_dir = root / "bin"
            shim_dir.mkdir()
            shim = shim_dir / "codex"
            shim.write_text(
                "#!/bin/sh\n"
                f'exec "{sys.executable}" "{_FAKE}" turn_failed\n',
                encoding="utf-8",
            )
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
            environment = dict(os.environ)
            environment["PATH"] = f"{shim_dir}{os.pathsep}{environment['PATH']}"
            buffer = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False):
                with redirect_stdout(buffer):
                    code = soak.main(
                        [
                            "--iterations",
                            "2",
                            "--artifacts-dir",
                            str(root / "artifacts"),
                        ]
                    )
            output = buffer.getvalue()
            self.assertEqual(code, 1, output)
            self.assertIn("Codex turn failures  2", output)
            self.assertIn("Unknown failures     0", output)


if __name__ == "__main__":
    unittest.main()
