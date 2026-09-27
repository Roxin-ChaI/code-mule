"""Retry notices are bounded diagnostics, not productive Worker activity."""

from dataclasses import replace
import unittest

from code_mule.worker import CodexTurnInactivityTimeout, CodexTurnTimeout

from .test_protocol import ScriptedMonotonic, make_client


_SECRET = "credential-canary-must-never-appear"


class RetryNotificationTimeoutTests(unittest.TestCase):
    def test_rc2_shaped_retry_stream_expires_at_inactivity_deadline(self):
        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "private prompt")
        client._config = replace(
            client._config,
            inactivity_timeout_seconds=120,
            max_turn_seconds=900,
        )
        messages = [
            {
                "method": "mcpServer/startupStatus/updated",
                "params": {"threadId": thread_id, "error": {"message": _SECRET}},
            },
            {
                "method": "turn/started",
                "params": {"threadId": thread_id, "turn": {"id": turn_id}},
            },
        ]
        messages.extend(
            {
                "method": "error",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "willRetry": True,
                    "error": {"message": _SECRET},
                },
            }
            for _ in range(100)
        )
        deadlines = []

        def next_message(deadline):
            deadlines.append(deadline)
            if messages:
                return messages.pop(0)
            raise CodexTurnTimeout("fake deadline reached")

        client._next_message = next_message
        client._monotonic = ScriptedMonotonic(0, 10)
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)

        diagnostics = client.transport_diagnostics()
        self.assertEqual(deadlines[:2], [120, 120])
        self.assertTrue(all(deadline == 130 for deadline in deadlines[2:]))
        self.assertEqual(diagnostics.activity_count, 1)
        self.assertEqual(client.retryable_error_count, 100)
        self.assertEqual(client.mcp_startup_error_count, 1)
        self.assertIsNotNone(client.first_mcp_startup_error_at)
        self.assertIsNotNone(client.last_retryable_error_at)
        self.assertIsNone(client.last_retryable_error_code)
        self.assertEqual(len(diagnostics.events), 20)
        self.assertTrue(all(
            event.event_type == "error"
            and event.payload_category == "retryable_turn_error"
            for event in diagnostics.events
        ))
        self.assertNotIn(_SECRET, repr(diagnostics))
        self.assertTrue(holder["process"].terminated)
        self.assertEqual(diagnostics.cleanup_reason, "timeout")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
