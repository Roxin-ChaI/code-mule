"""Deterministic checks for the Boss-only RC2 Worker probe (no real Codex)."""

import importlib.util
import io
import json
from datetime import UTC, datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stderr, redirect_stdout

from code_mule.domain.models import ExecutionReport
from code_mule.transport import ChannelState, TransportDiagnostics
from code_mule.worker import CodexTurnFailed, CodexTurnFailureDetails, CodexTurnFailureKind, CodexTurnInactivityTimeout
from code_mule.worker.upstream import (
    UpstreamDiagnostics, UpstreamErrorObservation, UpstreamErrorSummary, safe_error_info,
)


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "manual_rc2_worker_probe.py"
_SECRET = "credential-canary-never-write"


def load_probe():
    spec = importlib.util.spec_from_file_location("manual_rc2_worker_probe", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["manual_rc2_worker_probe"] = module
    spec.loader.exec_module(module)
    return module


class FakeSession:
    instances = []
    failure = None

    def __init__(self, config):
        self.config = config
        self.started = 0
        self.executions = 0
        self.closed = 0
        self.retryable_error_count = 7
        self.mcp_startup_error_count = 2
        self.last_retryable_error_code = "internal_error"
        self.first_retryable_error_at = datetime(2026, 9, 27, 10, 17, tzinfo=UTC)
        self.last_retryable_error_at = datetime(2026, 9, 27, 10, 18, tzinfo=UTC)
        self.first_mcp_startup_error_at = datetime(2026, 9, 27, 10, 16, tzinfo=UTC)
        self.last_mcp_startup_error_at = datetime(2026, 9, 27, 10, 19, tzinfo=UTC)
        self.__class__.instances.append(self)

    def start(self):
        self.started += 1

    def execute(self, request, *, report_id, created_at):
        self.executions += 1
        if self.failure is not None:
            raise self.failure
        (self.config.workspace / "CONTRACT.md").write_text(
            "# Contract\nserver.py:4 Handler; host 127.0.0.1, port 8000\n",
            encoding="utf-8",
        )
        return ExecutionReport(
            report_id, request.task.id, 1, "completed", ("CONTRACT.md",),
            ("py_compile passed",), (), "dirty", (), None, "safe", created_at,
        )

    def close(self):
        self.closed += 1

    def upstream_diagnostics(self):
        return getattr(self.failure, "upstream", None) or UpstreamDiagnostics()

    def transport_diagnostics(self):
        return TransportDiagnostics(
            stdout_state=ChannelState.CLOSED,
            stderr_state=ChannelState.CLOSED,
            stdin_state=ChannelState.CLOSED,
            terminal_event_received=False,
            activity_count=0,
            cleanup_reason="timeout" if self.failure is not None else "normal",
        )


class ManualRc2ProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = load_probe()
        FakeSession.instances = []
        FakeSession.failure = None

    def test_config_matches_rc2_and_not_soak(self):
        config = self.probe.worker_config(Path("/tmp/disposable"))
        self.assertEqual(config.command, ("codex", "app-server"))
        self.assertEqual(config.approval_policy, "on-request")
        self.assertEqual(config.sandbox, "workspace-write")
        self.assertEqual(config.inactivity_timeout_seconds, 120)
        self.assertEqual(config.max_turn_seconds, 900)
        self.assertEqual(self.probe._CONFIG["model"], "inherited_from_codex_config")

    def test_one_fake_turn_preserves_disposable_workspace_and_safe_artifact(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            with redirect_stdout(output):
                result = self.probe.run_probe(root, session_factory=FakeSession)
            self.assertEqual(result, 0)
            self.assertEqual(len(FakeSession.instances), 1)
            session = FakeSession.instances[0]
            self.assertEqual((session.started, session.executions, session.closed), (1, 1, 1))
            self.assertTrue((root / "workspace" / "CONTRACT.md").exists())
            artifact = root / "probe-diagnostics.json"
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(payload["outcome"], "passed")
            self.assertTrue(payload["git"]["head_unchanged"])
            self.assertEqual(payload["diagnostics"]["retryable_error_count"], 7)
            self.assertEqual(payload["diagnostics"]["mcp_startup_error_count"], 2)
            self.assertEqual(
                payload["diagnostics"]["first_retryable_error_at"],
                "2026-09-27T10:17:00+00:00",
            )
            self.assertEqual(
                payload["diagnostics"]["last_mcp_startup_error_at"],
                "2026-09-27T10:19:00+00:00",
            )
            self.assertEqual(payload["diagnostics"]["upstream_error_count"], 7)
            self.assertEqual(payload["diagnostics"]["local_timeout_count"], 0)
            self.assertNotIn(_SECRET, artifact.read_text(encoding="utf-8"))

    def test_timeout_is_local_and_preserves_failure_site_without_retry(self):
        FakeSession.failure = CodexTurnInactivityTimeout(
            _SECRET, inactivity_timeout_seconds=120, max_turn_seconds=900
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with redirect_stdout(io.StringIO()):
                result = self.probe.run_probe(root, session_factory=FakeSession)
            self.assertEqual(result, 1)
            self.assertEqual(len(FakeSession.instances), 1)
            session = FakeSession.instances[0]
            self.assertEqual((session.executions, session.closed), (1, 1))
            self.assertTrue((root / "workspace" / "server.py").exists())
            artifact = (root / "probe-diagnostics.json").read_text(encoding="utf-8")
            payload = json.loads(artifact)
            self.assertEqual(payload["diagnostics"]["timeout_kind"], "inactivity")
            self.assertEqual(payload["diagnostics"]["local_timeout_count"], 1)
            self.assertEqual(payload["diagnostics"]["upstream_error_count"], 7)
            self.assertNotIn(_SECRET, artifact)

    def test_missing_confirmation_does_not_start_worker(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                self.probe.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(FakeSession.instances, [])

    def test_final_upstream_probe_summary_is_safe_and_not_a_timeout(self):
        now = datetime(2026, 10, 2, 14, 21, tzinfo=UTC)
        retry = UpstreamErrorObservation(now, True, safe_error_info({
            "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 502}}
        }), "thread-1", "turn-1")
        final = UpstreamErrorObservation(now, False, safe_error_info({
            "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 503}}
        }), "thread-1", "turn-1")
        snapshot = UpstreamDiagnostics(
            UpstreamErrorSummary(4, retry, retry), UpstreamErrorSummary(1, final, final)
        )
        details = CodexTurnFailureDetails(
            CodexTurnFailureKind.ERROR_NOTIFICATION, "thread-1", "turn-1", None,
            False, final.info.code, 3, 112.5, 141.9, 503, snapshot,
        )
        FakeSession.failure = CodexTurnFailed(_SECRET, details=details)
        FakeSession.failure.upstream = snapshot
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            with redirect_stdout(output):
                result = self.probe.run_probe(root, session_factory=FakeSession)
            text = (root / "probe-diagnostics.json").read_text()
            diagnostics = json.loads(text)["diagnostics"]
            self.assertEqual(result, 1)
            self.assertEqual(diagnostics["upstream_retry_count"], 4)
            self.assertEqual(diagnostics["upstream_error_count"], 5)
            self.assertEqual(diagnostics["final_upstream_category"], "upstream_retry_exhaustion")
            self.assertEqual(diagnostics["final_upstream_code"], "responseTooManyFailedAttempts")
            self.assertEqual(diagnostics["inactivity_elapsed_since_last_valid_activity"], 112.5)
            self.assertFalse(diagnostics["timeout_triggered"])
            self.assertEqual(diagnostics["failure_boundary"], "final_upstream_turn_failure")
            self.assertEqual(diagnostics["mcp_startup_error_count"], 2)
            self.assertIn("Upstream retries: 4", output.getvalue())
            self.assertNotIn(_SECRET, text + output.getvalue())
            session = FakeSession.instances[0]
            self.assertEqual((session.executions, session.closed), (1, 1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
