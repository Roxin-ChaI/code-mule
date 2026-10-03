"""Safe upstream evidence and unchanged timeout semantics; no real Worker."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
import unittest

from code_mule.transport import TransportFailureKind, WorkerFailureClass
from code_mule.presentation.render import _render_worker_failure
from code_mule.worker import CodexTurnFailed, CodexTurnInactivityTimeout
from code_mule.worker.contracts import worker_failure_metadata, turn_failure_details_from_metadata
from code_mule.worker.upstream import (
    MAX_UPSTREAM_COUNT, MAX_UPSTREAM_JSON, OBJECT_ERROR_CODES, STRING_ERROR_CODES,
    UNAVAILABLE, SafeErrorInfo, UpstreamDiagnostics, UpstreamErrorObservation,
    UpstreamErrorSummary, safe_error_info, upstream_from_json,
)
from .test_protocol import make_client, ScriptedMonotonic
from .test_failure_diagnostics import notification, SECRET


NOW = datetime(2026, 10, 2, 14, 19, tzinfo=UTC)


def error(retryable, info):
    return notification("error", willRetry=retryable, error={
        "message": SECRET, "additionalDetails": SECRET,
        "codexErrorInfo": info, "response_body": SECRET,
    })


class UpstreamProjectionTests(unittest.TestCase):
    def test_real_schema_codes_and_http_status_are_projected(self):
        for code in STRING_ERROR_CODES:
            with self.subTest(code=code):
                info = safe_error_info({"codexErrorInfo": code, "message": SECRET})
                self.assertEqual(info.code, code)
                self.assertEqual(info.source, "codexErrorInfo")
        for code in OBJECT_ERROR_CODES:
            with self.subTest(code=code):
                info = safe_error_info({"codexErrorInfo": {code: {
                    "httpStatusCode": 503, "response_body": SECRET,
                }}})
                self.assertEqual(info.code, code)
                self.assertEqual(info.http_status_code, None if code == "activeTurnNotSteerable" else 503)
                self.assertNotIn(SECRET, repr(info))

    def test_unavailable_fallback_never_infers_cause_from_raw_message(self):
        for raw in (None, {}, {"message": SECRET + " exceeded retry limit 502"},
                    {"code": SECRET}, {"codexErrorInfo": SECRET},
                    {"codexErrorInfo": {SECRET: {"httpStatusCode": 502}}},
                    {"codexErrorInfo": {"httpConnectionFailed": SECRET}},
                    {"codexErrorInfo": "responseTooManyFailedAttempts"}):
            with self.subTest(shape=type(raw).__name__):
                info = safe_error_info(raw)
                self.assertEqual(info, SafeErrorInfo())
                self.assertNotIn(SECRET, repr(info))

    def test_http_status_is_bounded_and_official_info_has_priority(self):
        for status in (True, -1, 0, 99, 600, "503", SECRET, float("nan")):
            self.assertIsNone(safe_error_info({
                "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": status}}
            }).http_status_code)
        self.assertEqual(safe_error_info({
            "code": "internal_error", "codexErrorInfo": "unauthorized"
        }).code, "unauthorized")

    def test_summaries_are_bounded_round_trip_and_reject_tampering(self):
        obs = UpstreamErrorObservation(NOW, True, safe_error_info({
            "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 502}}
        }), "t" * 128, "u" * 128)
        summary = UpstreamErrorSummary(MAX_UPSTREAM_COUNT, obs, obs).append(obs)
        self.assertEqual(summary.count, MAX_UPSTREAM_COUNT)
        snapshot = UpstreamDiagnostics(summary)
        encoded = snapshot.to_json()
        self.assertLess(len(encoded), MAX_UPSTREAM_JSON)
        self.assertEqual(upstream_from_json(encoded), snapshot)
        for changes in ({"count": True}, {"count": -1}, {"count": MAX_UPSTREAM_COUNT + 1}):
            raw = snapshot.to_payload()
            raw["retryable"].update(changes)
            self.assertIsNone(upstream_from_json(json.dumps(raw)))
        for changes in ({"code": SECRET}, {"category": SECRET}, {"source": SECRET},
                        {"thread_id": "x" * 129}, {"at": "2026-10-02T00:00:00"}):
            raw = snapshot.to_payload()
            raw["retryable"]["last"].update(changes)
            self.assertIsNone(upstream_from_json(json.dumps(raw)))
        self.assertIsNone(upstream_from_json("x" * (MAX_UPSTREAM_JSON + 1)))
        raw = snapshot.to_payload()
        raw["retryable"]["last"]["raw_message"] = SECRET
        parsed = upstream_from_json(json.dumps(raw))
        self.assertNotIn(SECRET, parsed.to_json())

    def test_inspect_separates_prior_retry_errors_from_timeout_and_transport(self):
        observation = UpstreamErrorObservation(
            NOW, True, safe_error_info({"codexErrorInfo": "serverOverloaded"}),
            "thread-1", "turn-1",
        )
        snapshot = UpstreamDiagnostics(UpstreamErrorSummary(1, observation, observation))
        for boundary, metadata in (
            ("local_timeout", {"timeout_kind": "inactivity"}),
            ("transport_failure", {"failure_class": "transport_failure"}),
        ):
            with self.subTest(boundary=boundary):
                event = SimpleNamespace(
                    event_type="task.execution_failed", entity_id="task-1", timestamp=NOW,
                    metadata={**metadata, "upstream_diagnostics": snapshot.to_json(),
                              "raw_message": SECRET},
                )
                action = SimpleNamespace(task_id="task-1", created_at=NOW)
                output = "\n".join(_render_worker_failure(SimpleNamespace(events=(event,)), action))
                self.assertIn(f"Failure boundary {boundary}", output)
                self.assertIn("Retryable upstream error", output)
                self.assertIn("upstream_server_overloaded", output)
                self.assertNotIn("Final upstream turn failure", output)
                self.assertNotIn(SECRET, output)


class UpstreamClientTests(unittest.TestCase):
    def client(self):
        client, holder = make_client()
        client.initialize()
        client.start_thread()
        client.start_turn("thread-1", "private prompt")
        client._config = replace(client._config, inactivity_timeout_seconds=120, max_turn_seconds=900)
        self.addCleanup(client.close)
        return client, holder

    def test_rc2_shaped_final_failure_preserves_retry_history_and_activity_age(self):
        client, holder = self.client()
        messages = [
            notification("item/agentMessage/delta", itemId="item-1", delta=SECRET),
            error(True, {"responseStreamDisconnected": {"httpStatusCode": 502}}),
            error(True, {"responseStreamDisconnected": {"httpStatusCode": 503}}),
            error(False, {"responseTooManyFailedAttempts": {"httpStatusCode": 503}}),
        ]
        deadlines = []
        observed_time = [NOW]
        def next_message(hard, inactivity):
            deadlines.append((hard, inactivity))
            observed_time[0] += timedelta(seconds=1)
            return messages.pop(0)
        client._next_turn_message = next_message
        client._clock = lambda: observed_time[0]
        client._monotonic = ScriptedMonotonic(0, 22, 134.5)
        with self.assertRaises(CodexTurnFailed) as caught:
            client.wait_for_turn("thread-1", "turn-1")
        failure = caught.exception
        self.assertEqual(failure.details.kind.value, "error_notification")
        self.assertEqual(failure.details.error_code, "responseTooManyFailedAttempts")
        self.assertEqual(failure.details.http_status_code, 503)
        self.assertEqual(failure.details.activity_count, 1)
        self.assertEqual(failure.details.last_activity_age_seconds, 112.5)
        self.assertEqual(deadlines, [(900, 120), (900, 142), (900, 142), (900, 142)])
        snapshot = failure.details.upstream
        self.assertEqual(snapshot.retryable.count, 2)
        self.assertEqual(snapshot.retryable.first.at, NOW + timedelta(seconds=2))
        self.assertEqual(snapshot.retryable.last.at, NOW + timedelta(seconds=3))
        self.assertEqual(snapshot.final.count, 1)
        self.assertFalse(snapshot.final.last.retryable)
        self.assertEqual(snapshot.final.last.info.category, "upstream_retry_exhaustion")
        metadata = worker_failure_metadata(failure)
        self.assertNotIn(SECRET, json.dumps(metadata))
        self.assertEqual(turn_failure_details_from_metadata(metadata), failure.details)
        self.assertIs(failure.failure_class, WorkerFailureClass.CODEX_TURN_FAILURE)
        self.assertIs(client.transport_diagnostics().transport_failure_kind, TransportFailureKind.ERROR_NOTIFICATION)
        self.assertFalse(holder["process"].terminated)

    def test_final_error_with_no_safe_code_is_still_upstream_failure(self):
        client, _ = self.client()
        client._pending_messages.append(error(False, None))
        with self.assertRaises(CodexTurnFailed) as caught:
            client.wait_for_turn("thread-1", "turn-1")
        snapshot = caught.exception.details.upstream
        self.assertEqual(snapshot.final.last.info.category, UNAVAILABLE)
        self.assertEqual(worker_failure_metadata(caught.exception)["error_category"], UNAVAILABLE)
        self.assertNotIn(SECRET, snapshot.to_json())

    def test_timeout_after_retry_retains_retry_evidence_and_timeout_class(self):
        client, _ = self.client()
        messages = [error(True, "serverOverloaded")]
        def next_message(hard, inactivity):
            if messages:
                return messages.pop(0)
            raise CodexTurnInactivityTimeout("fake timeout")
        client._next_turn_message = next_message
        with self.assertRaises(CodexTurnInactivityTimeout) as caught:
            client.wait_for_turn("thread-1", "turn-1")
        metadata = worker_failure_metadata(caught.exception)
        snapshot = upstream_from_json(metadata["upstream_diagnostics"])
        self.assertEqual(snapshot.retryable.count, 1)
        self.assertIsNone(snapshot.final)
        self.assertEqual(metadata["timeout_kind"], "inactivity")

    def test_transport_failure_after_retry_remains_transport_failure(self):
        client, _ = self.client()
        messages = [error(True, "serverOverloaded")]
        def next_message(hard, inactivity):
            if messages:
                return messages.pop(0)
            raise client._fail(TransportFailureKind.STDOUT_EOF, "fake EOF")
        client._next_turn_message = next_message
        with self.assertRaises(Exception) as caught:
            client.wait_for_turn("thread-1", "turn-1")
        self.assertIs(caught.exception.failure_class, WorkerFailureClass.TRANSPORT_FAILURE)
        snapshot = upstream_from_json(worker_failure_metadata(caught.exception)["upstream_diagnostics"])
        self.assertEqual(snapshot.retryable.count, 1)
        self.assertIsNone(snapshot.final)

    def test_new_turn_does_not_reuse_previous_upstream_evidence(self):
        client, _ = self.client()
        client._pending_messages.append(error(False, "unauthorized"))
        with self.assertRaises(CodexTurnFailed):
            client.wait_for_turn("thread-1", "turn-1")
        client.start_turn("thread-1", "another private prompt")
        self.assertEqual(client.upstream_diagnostics(), UpstreamDiagnostics())


if __name__ == "__main__":
    unittest.main()
