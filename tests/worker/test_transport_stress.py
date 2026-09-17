"""Deterministic Codex Worker transport supervision and stress coverage.

Every test here drives a real child process over real stdio pipes using the
``fake_codex_app_server`` fixture.  No test starts a real Codex turn, touches
the network, or reads model output.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from code_mule.transport import (
    ChannelState,
    TransportDiagnostics,
    TransportFailureKind,
    WorkerFailureClass,
    legacy_transport_diagnostics,
)
from code_mule.worker import (
    CodexAppServerDisconnected,
    CodexRequestRejected,
    CodexStdoutReaderFailed,
    CodexTurnFailed,
    CodexTurnFailureKind,
    CodexTurnHardTimeout,
    CodexTurnInactivityTimeout,
    CodexWorkerConfig,
    CodexWorkerError,
    InvalidWorkerReport,
)
from code_mule.worker.client import CodexAppServerClient
from code_mule.worker.contracts import worker_failure_metadata
from code_mule.worker.service import CodexWorkerSession

FAKE_SERVER = Path(__file__).with_name("fake_codex_app_server.py")


def make_config(workspace: Path, *, inactivity: float = 2.0, maximum: float = 8.0):
    return CodexWorkerConfig(
        command=(sys.executable, str(FAKE_SERVER), "normal_completion"),
        workspace=workspace,
        approval_policy="on-request",
        sandbox="read-only",
        inactivity_timeout_seconds=inactivity,
        max_turn_seconds=maximum,
    )


def make_client(scenario: str, workspace: Path, *, inactivity: float = 2.0, maximum: float = 8.0, eof_grace: float = 0.15):
    config = replace(
        make_config(workspace, inactivity=inactivity, maximum=maximum),
        command=(sys.executable, str(FAKE_SERVER), scenario),
    )
    return CodexAppServerClient(config, eof_grace_seconds=eof_grace)


class TransportScenarioTests(unittest.TestCase):
    """One subprocess per scenario, asserting a typed and bounded outcome."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def client(self, scenario: str, **kwargs) -> CodexAppServerClient:
        client = make_client(scenario, self.workspace, **kwargs)
        self.addCleanup(self._assert_no_orphan, client)
        return client

    def _assert_no_orphan(self, client: CodexAppServerClient) -> None:
        process = client._process
        client.close(reason="test_cleanup")
        if process is not None:
            deadline = time.monotonic() + 2
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(process.poll(), "fake app-server was not reaped")
        self.assertEqual(client._reader_threads, [])
        for thread in threading.enumerate():
            self.assertFalse(
                thread.name.startswith("code-mule-codex-") and thread.is_alive(),
                f"leaked reader thread {thread.name}",
            )

    def run_turn(self, scenario: str, **kwargs):
        client = self.client(scenario, **kwargs)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        return client, client.wait_for_turn(thread_id, turn_id)

    # -- 1. normal turn completion -----------------------------------------

    def test_normal_completion_produces_a_trusted_report(self) -> None:
        client, result = self.run_turn("normal_completion")
        self.assertTrue(result.completed)
        self.assertIn('"status": "completed"', result.final_message or "")
        self.assertIsNotNone(client.terminal_evidence)
        self.assertEqual(client.terminal_evidence.turn_status, "completed")
        diagnostics = client.transport_diagnostics()
        self.assertIsNone(diagnostics.transport_failure_kind)
        self.assertTrue(diagnostics.terminal_event_received)
        self.assertEqual(diagnostics.terminal_event_type, "turn/completed")
        self.assertGreaterEqual(diagnostics.activity_count, 2)
        self.assertTrue(diagnostics.events)
        self.assertTrue(
            all(event.payload_category != "raw" for event in diagnostics.events)
        )
        client.close(reason="completed")
        self.assertIsNotNone(client.transport_diagnostics().app_server_exit_code)

    # -- 2/3/4. explicit Codex-side failures --------------------------------

    def test_turn_failed_is_a_typed_codex_turn_failure(self) -> None:
        client = self.client("turn_failed")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexTurnFailed) as caught:
            client.wait_for_turn(thread_id, turn_id)
        self.assertIs(caught.exception.details.kind, CodexTurnFailureKind.TURN_FAILED)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind, TransportFailureKind.TURN_FAILED
        )
        self.assertIs(
            diagnostics.failure_class, WorkerFailureClass.CODEX_TURN_FAILURE
        )
        self.assertEqual(
            worker_failure_metadata(caught.exception)["failure_class"],
            "codex_turn_failure",
        )

    def test_error_notification_is_a_typed_codex_turn_failure(self) -> None:
        client = self.client("error_notification")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexTurnFailed) as caught:
            client.wait_for_turn(thread_id, turn_id)
        self.assertIs(
            caught.exception.details.kind, CodexTurnFailureKind.ERROR_NOTIFICATION
        )
        self.assertIs(
            client.transport_diagnostics().transport_failure_kind,
            TransportFailureKind.ERROR_NOTIFICATION,
        )
        self.assertEqual(
            worker_failure_metadata(caught.exception)["failure_class"],
            "codex_turn_failure",
        )

    def test_alternate_terminal_method_is_classified_not_timed_out(self) -> None:
        client = self.client("turn_failed_method")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexTurnFailed) as caught:
            client.wait_for_turn(thread_id, turn_id)
        self.assertIs(caught.exception.details.kind, CodexTurnFailureKind.TURN_FAILED)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind, TransportFailureKind.TURN_FAILED
        )
        self.assertTrue(diagnostics.terminal_event_received)
        self.assertEqual(diagnostics.terminal_event_type, "turn/failed")

    def test_request_rejection_is_never_an_unknown_stop(self) -> None:
        client = self.client("request_rejected")
        client.initialize()
        thread_id = client.start_thread()
        with self.assertRaises(CodexRequestRejected) as caught:
            client.start_turn(thread_id, "prompt")
        self.assertEqual(caught.exception.request_id, 3)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind, TransportFailureKind.REQUEST_REJECTED
        )
        self.assertEqual(diagnostics.request_id, 3)

    # -- 5/6/7/16. process exit versus channel EOF -------------------------

    def test_stdout_eof_with_exit_zero_is_classified_as_channel_eof(self) -> None:
        client = self.client("stdout_eof_exit_zero")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexAppServerDisconnected):
            client.wait_for_turn(thread_id, turn_id)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind, TransportFailureKind.STDOUT_EOF
        )
        self.assertIs(diagnostics.stdout_state, ChannelState.EOF)
        self.assertIs(diagnostics.process_alive_at_failure, False)
        self.assertEqual(diagnostics.app_server_exit_code, 0)
        self.assertEqual(diagnostics.failure_class, WorkerFailureClass.TRANSPORT_FAILURE)

    def test_stdout_eof_with_nonzero_exit_captures_the_real_exit_code(self) -> None:
        client = self.client("stdout_eof_exit_nonzero")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexAppServerDisconnected):
            client.wait_for_turn(thread_id, turn_id)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind, TransportFailureKind.PROCESS_EXITED
        )
        self.assertEqual(diagnostics.app_server_exit_code, 7)
        self.assertEqual(
            diagnostics.failure_class, WorkerFailureClass.CODEX_PROCESS_FAILURE
        )

    def test_stdout_eof_while_process_alive_is_a_disconnect_not_an_unknown(self) -> None:
        client = self.client("stdout_eof_while_alive", eof_grace=0.1)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexAppServerDisconnected):
            client.wait_for_turn(thread_id, turn_id)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind,
            TransportFailureKind.APP_SERVER_DISCONNECTED,
        )
        self.assertIs(diagnostics.process_alive_at_failure, True)
        self.assertEqual(diagnostics.failure_class, WorkerFailureClass.TRANSPORT_FAILURE)

    def test_signal_termination_records_the_signal(self) -> None:
        client = self.client("signal_termination")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexWorkerError):
            client.wait_for_turn(thread_id, turn_id)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind, TransportFailureKind.PROCESS_EXITED
        )
        self.assertEqual(diagnostics.app_server_exit_signal, 9)

    # -- 8. stderr backpressure --------------------------------------------

    def test_stderr_burst_never_deadlocks_and_stays_bounded(self) -> None:
        client, result = self.run_turn("stderr_burst", inactivity=10.0, maximum=30.0)
        self.assertTrue(result.completed)
        self.assertLessEqual(len(client.stderr_tail), 100)
        self.assertEqual(client.transport_diagnostics().stderr_state, ChannelState.OPEN)

    # -- 9/10. reader and framing failures ---------------------------------

    def test_stdout_reader_failure_propagates_and_is_classified(self) -> None:
        client = self.client("stdout_reader_exception_delayed")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexStdoutReaderFailed):
            client.wait_for_turn(thread_id, turn_id)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind,
            TransportFailureKind.STDOUT_READER_FAILED,
        )
        self.assertIs(diagnostics.stdout_state, ChannelState.FAILED)
        self.assertIsNotNone(diagnostics.reader_failure_kind)

    def test_corrupt_bytes_in_a_read_chunk_are_still_a_typed_reader_failure(self) -> None:
        # A bad byte invalidates the whole text-mode read chunk, so the failure
        # may surface on whichever call is reading.  It must still be typed.
        client = self.client("stdout_reader_exception")
        client.initialize()
        thread_id = client.start_thread()
        with self.assertRaises(CodexStdoutReaderFailed):
            turn_id = client.start_turn(thread_id, "prompt")
            client.wait_for_turn(thread_id, turn_id)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind,
            TransportFailureKind.STDOUT_READER_FAILED,
        )

    def test_malformed_jsonrpc_is_a_decode_failure(self) -> None:
        client = self.client("malformed_jsonrpc")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexWorkerError):
            client.wait_for_turn(thread_id, turn_id)
        self.assertIs(
            client.transport_diagnostics().transport_failure_kind,
            TransportFailureKind.JSONRPC_DECODE_FAILED,
        )

    def test_oversized_frame_is_ignored_not_fatal(self) -> None:
        client = self.client("oversized_line", inactivity=0.4, maximum=5.0)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)

    # -- 11/13/14. timing --------------------------------------------------

    def test_delayed_terminal_event_is_not_a_timeout(self) -> None:
        client, result = self.run_turn("delayed_terminal", inactivity=5.0, maximum=20.0)
        self.assertTrue(result.completed)

    def test_inactivity_timeout_is_typed(self) -> None:
        client = self.client("inactivity_timeout", inactivity=0.4, maximum=5.0)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind,
            TransportFailureKind.INACTIVITY_TIMEOUT,
        )
        self.assertEqual(diagnostics.failure_class, WorkerFailureClass.TIMEOUT)

    def test_hard_timeout_is_typed(self) -> None:
        client = self.client("hard_timeout", inactivity=1.0, maximum=1.0)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        with self.assertRaises(CodexTurnHardTimeout):
            client.wait_for_turn(thread_id, turn_id)
        self.assertIs(
            client.transport_diagnostics().transport_failure_kind,
            TransportFailureKind.HARD_TIMEOUT,
        )

    # -- 12. terminal evidence survives a report parse failure --------------

    def test_terminal_evidence_survives_a_report_parse_failure(self) -> None:
        client = self.client("terminal_then_report_parse_failure")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        result = client.wait_for_turn(thread_id, turn_id)
        terminal = client.terminal_evidence
        self.assertIsNotNone(terminal)
        self.assertTrue(terminal.final_message_present)
        report_error = InvalidWorkerReport("rejected", terminal=terminal)
        metadata = worker_failure_metadata(report_error)
        self.assertEqual(metadata["terminal_event_received"], "true")
        self.assertEqual(metadata["report_parse_failed"], "true")
        self.assertEqual(metadata["terminal_event_type"], "turn/completed")
        self.assertEqual(metadata["thread_id"], result.thread_id)
        self.assertIs(
            report_error.transport_failure_kind,
            TransportFailureKind.REPORT_PARSE_FAILED,
        )

    # -- 17/18/19. unrelated, duplicate, and late events --------------------

    def test_unrelated_and_unknown_notifications_do_not_break_the_transport(self) -> None:
        client, result = self.run_turn("unrelated_notification")
        self.assertTrue(result.completed)
        self.assertIsNone(client.transport_diagnostics().transport_failure_kind)

    def test_duplicate_terminal_event_is_ignored(self) -> None:
        client, result = self.run_turn("duplicate_terminal")
        self.assertTrue(result.completed)
        client.close(reason="duplicate")
        self.assertEqual(client.transport_diagnostics().cleanup_reason, "duplicate")

    def test_late_event_from_an_old_turn_is_ignored(self) -> None:
        client, result = self.run_turn("late_event_from_old_turn")
        self.assertTrue(result.completed)

    # -- 15/20. cancellation and clean shutdown -----------------------------

    def test_parent_interrupt_keeps_typed_evidence_and_reaps_the_child(self) -> None:
        client = self.client("inactivity_timeout", inactivity=30.0, maximum=60.0)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        self.assertIsNotNone(turn_id)
        original = client._next_turn_message

        def interrupted(*_args, **_kwargs):
            raise KeyboardInterrupt

        client._next_turn_message = interrupted  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            client.wait_for_turn(thread_id, turn_id)
        client._next_turn_message = original  # type: ignore[method-assign]
        diagnostics = client.transport_diagnostics()
        self.assertIs(
            diagnostics.transport_failure_kind,
            TransportFailureKind.PARENT_INTERRUPTED,
        )
        self.assertEqual(
            diagnostics.failure_class, WorkerFailureClass.USER_INTERRUPT
        )
        self.assertEqual(client.transport_diagnostics().cleanup_reason, "parent_interrupted")

    def test_clean_shutdown_leaves_no_process_or_thread(self) -> None:
        client, result = self.run_turn("clean_shutdown")
        self.assertTrue(result.completed)
        process = client._process
        client.close(reason="clean_shutdown")
        self.assertIsNotNone(process.poll())
        self.assertEqual(client.transport_diagnostics().cleanup_reason, "clean_shutdown")


class TransportStressTests(unittest.TestCase):
    """Repeated deterministic cycles must never leak or lose a terminal."""

    ITERATIONS = int(os.environ.get("CODE_MULE_TRANSPORT_STRESS_ITERATIONS", "120"))

    def test_repeated_worker_cycles_are_deterministic(self) -> None:
        terminals = 0
        failures = 0
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for _ in range(self.ITERATIONS):
                client = make_client("normal_completion", workspace)
                try:
                    client.initialize()
                    thread_id = client.start_thread()
                    turn_id = client.start_turn(thread_id, "prompt")
                    result = client.wait_for_turn(thread_id, turn_id)
                    self.assertTrue(result.completed)
                    terminals += 1
                except CodexWorkerError:  # pragma: no cover - failure evidence
                    failures += 1
                finally:
                    client.close(reason="stress")
                diagnostics = client.transport_diagnostics()
                self.assertEqual(diagnostics.transport_failure_kind, None)
                self.assertEqual(client._reader_threads, [])
                self.assertIsNotNone(diagnostics.app_server_exit_code)
        self.assertEqual(terminals, self.ITERATIONS)
        self.assertEqual(failures, 0)

    def test_reader_threads_are_never_left_behind(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for scenario in ("normal_completion", "stderr_burst", "unrelated_notification"):
                client = make_client(scenario, workspace, inactivity=10.0, maximum=30.0)
                client.initialize()
                thread_id = client.start_thread()
                turn_id = client.start_turn(thread_id, "prompt")
                client.wait_for_turn(thread_id, turn_id)
                client.close(reason="stress")
                self.assertEqual(client._reader_threads, [])
                self.assertTrue(
                    all(
                        not (
                            thread.name.startswith("code-mule-codex-")
                            and thread.is_alive()
                        )
                        for thread in threading.enumerate()
                    )
                )


class LongTurnClockTests(unittest.TestCase):
    """Valid activity refreshes inactivity but never the hard deadline."""

    def test_valid_activity_refreshes_inactivity_but_not_the_hard_deadline(self) -> None:
        from .test_protocol import ScriptedMonotonic, make_client

        client, holder = make_client(timeout=120)
        self.addCleanup(client.close)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        client._config = replace(
            client._config, max_turn_seconds=300.0, inactivity_timeout_seconds=30.0
        )
        # Five simulated minutes of activity at 20s intervals: every interval is
        # shorter than the 30s inactivity window, so the hard deadline must fire.
        client._monotonic = ScriptedMonotonic(*[0.0] + [20.0 * n for n in range(1, 21)])
        for _ in range(20):
            holder["process"].stdout.emit(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {"type": "commandExecution", "id": "cmd"},
                    },
                }
            )
        with self.assertRaises(CodexTurnHardTimeout):
            client.wait_for_turn(thread_id, turn_id)
        self.assertIs(
            client.transport_diagnostics().transport_failure_kind,
            TransportFailureKind.HARD_TIMEOUT,
        )


class WorkerSessionHandoffTests(unittest.TestCase):
    """The session boundary must keep terminal evidence for the hand-off."""

    def _task(self):
        from code_mule.domain.models import Task
        from code_mule.domain.enums import TaskStatus

        return Task(
            id="TASK-001",
            milestone_id="MILESTONE-001",
            title="Fake transport task",
            description="Exercise the Worker transport boundary deterministically.",
            status=TaskStatus.IN_PROGRESS,
            dependencies=(),
            acceptance_criteria=(),
            execution_attempts=0,
            created_at=datetime(2026, 9, 17, tzinfo=UTC),
            updated_at=datetime(2026, 9, 17, tzinfo=UTC),
        )

    def _session(self, scenario: str, workspace: Path) -> CodexWorkerSession:
        config = replace(
            make_config(workspace),
            command=(sys.executable, str(FAKE_SERVER), scenario),
        )
        session = CodexWorkerSession(
            config,
            client_factory=lambda _config: CodexAppServerClient(config, eof_grace_seconds=0.1),
        )
        self.addCleanup(session.close)
        return session

    def _execute(self, scenario: str):
        from code_mule.worker.contracts import WorkerTaskRequest

        with TemporaryDirectory() as tmp:
            session = self._session(scenario, Path(tmp))
            session.start()
            request = WorkerTaskRequest(self._task(), "prompt", "Fake transport task")
            return session.execute(
                request,
                report_id="report-1",
                created_at=datetime(2026, 9, 17, tzinfo=UTC),
            )

    def _expect_report_failure(self, scenario: str):
        from code_mule.worker.contracts import WorkerTaskRequest

        with TemporaryDirectory() as tmp:
            session = self._session(scenario, Path(tmp))
            session.start()
            request = WorkerTaskRequest(self._task(), "prompt", "Fake transport task")
            with self.assertRaises(InvalidWorkerReport) as caught:
                session.execute(
                    request,
                    report_id="report-1",
                    created_at=datetime(2026, 9, 17, tzinfo=UTC),
                )
            return caught.exception

    def test_prose_plus_json_fence_is_accepted(self) -> None:
        """The exact envelope that broke real soak iterations 02 and 08."""

        report = self._execute("wrapped_report")
        self.assertEqual(report.status, "completed")
        self.assertIn("value.txt", report.files_changed)
        self.assertTrue(report.tests)

    def test_prose_plus_embedded_object_is_accepted(self) -> None:
        report = self._execute("embedded_report")
        self.assertEqual(report.status, "completed")

    def test_ambiguous_report_candidates_fail_closed(self) -> None:
        from code_mule.worker.report_contract import (
            ReportFailureStage,
            ReportValidationCode,
        )

        error = self._expect_report_failure("ambiguous_report")
        self.assertIs(error.stage, ReportFailureStage.EXTRACTION)
        self.assertIs(error.code, ReportValidationCode.AMBIGUOUS_JSON_CANDIDATE)
        self.assertTrue(error.candidate_found)
        self.assertFalse(error.json_decoded)
        metadata = worker_failure_metadata(error)
        self.assertEqual(metadata["report_stage"], "extraction")
        self.assertEqual(
            metadata["report_code"], "ambiguous_json_candidate"
        )
        self.assertEqual(metadata["failure_class"], "code_mule_runtime_failure")

    def test_prose_without_json_is_a_typed_extraction_failure(self) -> None:
        from code_mule.worker.report_contract import (
            ReportFailureStage,
            ReportValidationCode,
        )

        error = self._expect_report_failure("prose_only_report")
        self.assertIs(error.stage, ReportFailureStage.EXTRACTION)
        self.assertIs(error.code, ReportValidationCode.NO_JSON_CANDIDATE)
        self.assertIsNotNone(error.terminal)
        self.assertTrue(error.terminal.final_message_present)
        metadata = worker_failure_metadata(error)
        self.assertEqual(metadata["terminal_event_received"], "true")
        self.assertEqual(metadata["report_parse_failed"], "true")

    def test_completed_turn_with_a_rejected_report_keeps_terminal_evidence(self) -> None:
        from code_mule.worker.contracts import WorkerTaskRequest

        with TemporaryDirectory() as tmp:
            session = self._session("terminal_then_report_parse_failure", Path(tmp))
            session.start()
            request = WorkerTaskRequest(self._task(), "prompt", "Fake transport task")
            with self.assertRaises(InvalidWorkerReport) as caught:
                session.execute(
                    request,
                    report_id="report-1",
                    created_at=datetime(2026, 9, 17, tzinfo=UTC),
                )
            terminal = caught.exception.terminal
            self.assertIsNotNone(terminal)
            self.assertEqual(terminal.turn_status, "completed")
            self.assertEqual(terminal.terminal_event_type, "turn/completed")
            self.assertTrue(terminal.final_message_present)
            diagnostics = session.transport_diagnostics()
            self.assertTrue(diagnostics.terminal_event_received)
            self.assertIsNone(diagnostics.transport_failure_kind)
            metadata = worker_failure_metadata(caught.exception)
            self.assertEqual(metadata["terminal_event_received"], "true")
            self.assertEqual(metadata["report_parse_failed"], "true")

    def test_successful_turn_persists_turn_identity_and_healthy_diagnostics(self) -> None:
        from code_mule.worker.contracts import WorkerTaskRequest

        with TemporaryDirectory() as tmp:
            session = self._session("normal_completion", Path(tmp))
            session.start()
            report = session.execute(
                WorkerTaskRequest(self._task(), "prompt", "Fake transport task"),
                report_id="report-1",
                created_at=datetime(2026, 9, 17, tzinfo=UTC),
            )
            self.assertEqual(report.status, "completed")
            self.assertEqual(session.turn_id, "turn-fake-1")
            diagnostics = session.transport_diagnostics()
            self.assertTrue(diagnostics.terminal_event_received)
            self.assertIsNone(diagnostics.transport_failure_kind)
            session.close()
            closed = session.transport_diagnostics()
            self.assertIsNotNone(closed.app_server_exit_code)
            self.assertEqual(closed.cleanup_reason, "closed")


class TransportDiagnosticsContractTests(unittest.TestCase):
    def test_diagnostics_reject_unbounded_or_inconsistent_fields(self) -> None:
        diagnostics = legacy_transport_diagnostics()
        self.assertTrue(diagnostics.legacy_transport_evidence_incomplete)
        with self.assertRaises(ValueError):
            replace(
                diagnostics,
                transport_failure_kind=TransportFailureKind.PROCESS_EXITED,
            )
        with self.assertRaises(ValueError):
            replace(diagnostics, terminal_event_received=True)
        with self.assertRaises(ValueError):
            replace(diagnostics, activity_count=5)
        with self.assertRaises(ValueError):
            replace(diagnostics, last_protocol_event_type="x" * 200)
        with self.assertRaises(ValueError):
            replace(diagnostics, app_server_pid=-1)
        typed = replace(
            diagnostics,
            transport_failure_kind=TransportFailureKind.PROCESS_EXITED,
            failure_class=WorkerFailureClass.CODEX_PROCESS_FAILURE,
        )
        self.assertIsNotNone(typed)

    def test_diagnostics_never_hold_raw_payload_fields(self) -> None:
        fields = set(TransportDiagnostics.__dataclass_fields__)
        for forbidden in (
            "stdout",
            "stderr",
            "prompt",
            "reasoning",
            "raw_output",
            "response",
        ):
            self.assertNotIn(forbidden, fields)


if __name__ == "__main__":
    unittest.main()
