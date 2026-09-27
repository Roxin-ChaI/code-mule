"""Safe failure evidence from deterministic App Server notifications."""

from dataclasses import replace
import unittest

from code_mule.worker import (
    CodexProtocolError, CodexTurnFailed, CodexTurnFailureDetails,
    CodexTurnFailureKind,
)
from code_mule.worker.contracts import worker_failure_metadata, turn_failure_details_from_metadata
from .test_protocol import make_client, ScriptedMonotonic


SECRET = "credential-canary-never-persist"


def notification(method, **params):
    return {"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}}


def failure_message(kind):
    error = {"message": SECRET, "code": "internal_error", "credentials": SECRET}
    if kind is CodexTurnFailureKind.ERROR_NOTIFICATION:
        return notification("error", willRetry=False, error=error)
    status = "failed" if kind is CodexTurnFailureKind.TURN_FAILED else "interrupted"
    return notification("turn/completed", turn={"id": "turn-1", "status": status, "error": error})


class TurnFailureDiagnosticsTests(unittest.TestCase):
    def client(self):
        client, holder = make_client(timeout=100)
        client.initialize()
        client.start_thread()
        client.start_turn("thread-1", "private prompt")
        self.addCleanup(client.close)
        client._monotonic = ScriptedMonotonic(0, 10, 20, 30, 40)
        return client, holder

    def test_all_failure_sources_have_identity_and_exact_activity_timing(self):
        for kind in CodexTurnFailureKind:
            with self.subTest(kind=kind):
                client, _ = self.client()
                client._pending_messages.extend([
                    notification("turn/started", turn={"id": "turn-1"}),
                    notification("item/started", item={"type": "commandExecution"}),
                    failure_message(kind),
                ])
                with self.assertRaises(CodexTurnFailed) as caught:
                    client.wait_for_turn("thread-1", "turn-1")
                details = caught.exception.details
                self.assertIs(details.kind, kind)
                self.assertEqual((details.thread_id, details.turn_id), ("thread-1", "turn-1"))
                self.assertEqual(details.activity_count, 2)
                self.assertEqual(details.last_activity_age_seconds, 10)
                self.assertEqual(details.turn_elapsed_seconds, 30)
                self.assertEqual(details.error_code, "internal_error")
                self.assertNotIn(SECRET, str(caught.exception) + repr(details))

    def test_retryable_error_continues_without_counting_as_activity(self):
        client, _ = self.client()
        client._pending_messages.extend([
            notification("turn/started", turn={"id": "turn-1"}),
            notification("error", willRetry=True, error={"message": SECRET}),
            failure_message(CodexTurnFailureKind.TURN_FAILED),
        ])
        with self.assertRaises(CodexTurnFailed) as caught:
            client.wait_for_turn("thread-1", "turn-1")
        self.assertIs(caught.exception.details.kind, CodexTurnFailureKind.TURN_FAILED)
        self.assertEqual(caught.exception.details.activity_count, 1)

    def test_retryable_error_can_finish_successfully_without_raw_issue(self):
        client, _ = self.client()
        client._pending_messages.extend([
            notification("error", willRetry=True, error={"message": SECRET}),
            notification("item/completed", item={"type": "agentMessage", "text": "complete"}),
            notification("turn/completed", turn={"id": "turn-1", "status": "completed"}),
        ])
        result = client.wait_for_turn("thread-1", "turn-1")
        self.assertTrue(result.completed)
        self.assertNotIn(SECRET, repr(result))

    def test_safe_metadata_round_trip_rejects_tampered_diagnostic_fields(self):
        details = CodexTurnFailureDetails(
            CodexTurnFailureKind.ERROR_NOTIFICATION, "thread-1", "turn-1", None,
            False, "internal_error", 3, 12.4, 128.3,
        )
        metadata = worker_failure_metadata(CodexTurnFailed(details=details))
        self.assertEqual(turn_failure_details_from_metadata(metadata), details)
        self.assertNotIn("turn_status", metadata)
        self.assertEqual(turn_failure_details_from_metadata(metadata | {"raw_message": SECRET}), details)
        for changes in (
            {"error_code": SECRET}, {"activity_count": SECRET},
            {"last_activity_age_seconds": "nan"}, {"failure_kind": SECRET},
            {"turn_status": SECRET}, {"will_retry": "true"},
        ):
            self.assertIsNone(turn_failure_details_from_metadata(metadata | changes))

    def test_wrong_identity_does_not_fail_current_turn_or_refresh_activity(self):
        client, _ = self.client()
        client._pending_messages.extend([
            notification("error", threadId="other", willRetry=False, error={"message": SECRET}),
            notification("turn/completed", turn={"id": "other", "status": "failed"}),
            failure_message(CodexTurnFailureKind.TURN_INTERRUPTED),
        ])
        with self.assertRaises(CodexTurnFailed) as caught:
            client.wait_for_turn("thread-1", "turn-1")
        self.assertEqual(caught.exception.details.activity_count, 0)
        self.assertIsNone(caught.exception.details.last_activity_age_seconds)

    def test_malformed_error_and_retry_flag_are_protocol_failures(self):
        for params in (
            {"willRetry": False, "error": "invalid"},
            {"willRetry": False, "error": {"message": []}},
            {"willRetry": SECRET, "error": {}},
        ):
            with self.subTest(params=list(params)):
                client, _ = self.client()
                client._pending_messages.append(notification("error", **params))
                with self.assertRaises(CodexProtocolError) as caught:
                    client.wait_for_turn("thread-1", "turn-1")
                self.assertNotIn(SECRET, str(caught.exception))

    def test_unknown_or_secret_error_codes_are_omitted_without_prose_inference(self):
        for code in (SECRET, "x" * 10000, {"secret": SECRET}, None):
            client, _ = self.client()
            client._pending_messages.append(notification(
                "error", willRetry=False,
                error={"message": "internal_error " + SECRET, "code": code},
            ))
            with self.assertRaises(CodexTurnFailed) as caught:
                client.wait_for_turn("thread-1", "turn-1")
            self.assertIsNone(caught.exception.details.error_code)
            self.assertNotIn(SECRET, repr(caught.exception.details))

    def test_details_reject_unbounded_or_inconsistent_values(self):
        details = CodexTurnFailureDetails(
            CodexTurnFailureKind.TURN_FAILED, "thread-1", "turn-1", "failed",
            None, None, 1, 2.0, 10.0,
        )
        for changes in (
            {"thread_id": "x" * 129}, {"turn_id": "unsafe\nvalue"},
            {"error_code": SECRET}, {"activity_count": -1},
            {"turn_elapsed_seconds": float("nan")}, {"last_activity_age_seconds": 11},
            {"will_retry": True}, {"turn_status": "interrupted"},
        ):
            with self.subTest(fields=list(changes)), self.assertRaises(ValueError):
                replace(details, **changes)
        self.assertNotIn(SECRET, str(CodexTurnFailed(SECRET)))
