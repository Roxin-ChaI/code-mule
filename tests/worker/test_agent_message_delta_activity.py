"""Streaming agent text is activity; retry and unrelated events are not."""

from dataclasses import replace
import unittest

from code_mule.worker import (
    CodexProtocolError, CodexTurnHardTimeout, CodexTurnInactivityTimeout,
    CodexTurnTimeout,
)

from .test_protocol import ScriptedMonotonic, make_client


_SECRET = "private-stream-content-must-not-persist"


class AgentMessageDeltaActivityTests(unittest.TestCase):
    def client(self, *, hard_timeout=900):
        client, holder = make_client(timeout=0.2)
        self.addCleanup(client.close, reason="test_cleanup")
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "private prompt")
        client._config = replace(
            client._config,
            inactivity_timeout_seconds=120,
            max_turn_seconds=hard_timeout,
        )
        return client, holder, thread_id, turn_id

    @staticmethod
    def messages(client, *notifications):
        pending = list(notifications)
        deadlines = []

        def next_message(deadline):
            deadlines.append(deadline)
            if pending:
                return pending.pop(0)
            raise CodexTurnTimeout("fake deadline reached")

        client._next_message = next_message
        return deadlines

    @staticmethod
    def started(thread_id, turn_id):
        return {
            "method": "turn/started",
            "params": {"threadId": thread_id, "turn": {"id": turn_id}},
        }

    @staticmethod
    def delta(thread_id, turn_id, text=_SECRET):
        return {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": thread_id, "turnId": turn_id,
                "itemId": "msg-1", "delta": text,
            },
        }

    def test_current_nonempty_agent_delta_refreshes_inactivity_without_retaining_text(self):
        client, holder, thread_id, turn_id = self.client()
        deadlines = self.messages(
            client, self.started(thread_id, turn_id), self.delta(thread_id, turn_id)
        )
        client._monotonic = ScriptedMonotonic(0, 10, 100)
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)
        self.assertEqual(deadlines, [120, 130, 220])
        diagnostics = client.transport_diagnostics()
        self.assertEqual(diagnostics.activity_count, 2)
        self.assertEqual(diagnostics.events[-1].event_type, "item.agentmessage.delta")
        self.assertNotIn(_SECRET, repr(diagnostics))
        self.assertTrue(holder["process"].terminated)

    def test_wrong_turn_empty_delta_and_retry_only_never_refresh(self):
        client, _, thread_id, turn_id = self.client()
        deadlines = self.messages(
            client,
            self.delta(thread_id, "other-turn"),
            self.delta(thread_id, turn_id, ""),
            {
                "method": "error",
                "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "willRetry": True, "error": {"message": _SECRET},
                },
            },
        )
        client._monotonic = ScriptedMonotonic(0)
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)
        self.assertEqual(deadlines, [120, 120, 120, 120])
        self.assertEqual(client.transport_diagnostics().activity_count, 0)
        self.assertEqual(client.retryable_error_count, 1)

    def test_malformed_delta_fails_closed_without_retaining_payload(self):
        client, _, thread_id, turn_id = self.client()
        self.messages(client, self.delta(thread_id, turn_id, [_SECRET]))
        client._monotonic = ScriptedMonotonic(0)
        with self.assertRaises(CodexProtocolError) as caught:
            client.wait_for_turn(thread_id, turn_id)
        self.assertNotIn(_SECRET, str(caught.exception))

    def test_streaming_delta_cannot_extend_hard_deadline(self):
        client, _, thread_id, turn_id = self.client(hard_timeout=150)
        deadlines = self.messages(
            client, self.started(thread_id, turn_id), self.delta(thread_id, turn_id)
        )
        client._monotonic = ScriptedMonotonic(0, 10, 100)
        with self.assertRaises(CodexTurnHardTimeout):
            client.wait_for_turn(thread_id, turn_id)
        self.assertEqual(deadlines, [120, 130, 150])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
