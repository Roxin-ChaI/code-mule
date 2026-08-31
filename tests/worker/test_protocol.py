import io
import json
import queue
import subprocess
import threading
import unittest
from pathlib import Path

from code_mule.worker.client import CodexAppServerClient
from code_mule.worker.contracts import (
    CodexApprovalRequired,
    CodexProtocolError,
    CodexTurnFailed,
    CodexTurnTimeout,
    CodexUserInputRequired,
    CodexWorkerConfig,
)
from code_mule.worker.protocol import (
    MessageKind,
    classify_message,
    notification_message,
    request_message,
    response_result,
)


_CLOSE = object()


class FakeStdout:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self):
        item = self.lines.get()
        return "" if item is _CLOSE else item

    def emit(self, payload):
        if isinstance(payload, str):
            self.lines.put(payload + "\n")
        else:
            self.lines.put(json.dumps(payload) + "\n")

    def close(self):
        self.lines.put(_CLOSE)


class FakeStdin:
    def __init__(self, handler):
        self.handler = handler
        self.closed = False

    def write(self, value):
        self.handler(json.loads(value))
        return len(value)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, handler):
        self.stdout = FakeStdout()
        self.stderr = io.StringIO("")
        self.stdin = FakeStdin(lambda message: handler(self, message))
        self.exit_code = None
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = -15
        self.stdout.close()

    def kill(self):
        self.exit_code = -9
        self.stdout.close()

    def wait(self, timeout=None):
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.exit_code

    def exit(self, code=1):
        self.exit_code = code
        self.stdout.close()


def config(timeout=0.2):
    return CodexWorkerConfig(
        command=("codex", "app-server"),
        workspace=Path("/tmp/project"),
        approval_policy="on-request",
        sandbox="read-only",
        read_timeout_seconds=timeout,
    )


def standard_handler(process, message):
    method = message["method"]
    if method == "initialized":
        return
    if method == "initialize":
        process.stdout.emit({"id": message["id"], "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        process.stdout.emit(
            {"id": message["id"], "result": {"thread": {"id": "thread-1"}}}
        )
    elif method == "turn/start":
        process.stdout.emit(
            {"id": message["id"], "result": {"turn": {"id": "turn-1"}}}
        )


def make_client(handler=standard_handler, timeout=0.2):
    holder = {}

    def factory(*args, **kwargs):
        holder["args"] = args
        holder["kwargs"] = kwargs
        holder["process"] = FakeProcess(handler)
        return holder["process"]

    return CodexAppServerClient(config(timeout), popen_factory=factory), holder


class ProtocolHelperTests(unittest.TestCase):
    def test_builders_match_app_server_jsonl_envelopes(self):
        self.assertEqual(
            request_message(1, "initialize", {"clientInfo": {"name": "code-mule"}}),
            {
                "method": "initialize",
                "id": 1,
                "params": {"clientInfo": {"name": "code-mule"}},
            },
        )
        self.assertEqual(notification_message("initialized"), {"method": "initialized"})

    def test_classifies_response_notification_and_server_request(self):
        self.assertIs(classify_message({"id": 1, "result": {}}), MessageKind.RESPONSE)
        self.assertIs(
            classify_message({"method": "turn/completed", "params": {}}),
            MessageKind.NOTIFICATION,
        )
        self.assertIs(
            classify_message(
                {"id": 5, "method": "item/tool/requestUserInput", "params": {}}
            ),
            MessageKind.SERVER_REQUEST,
        )

    def test_invalid_envelopes_fail_closed(self):
        invalid = (
            [],
            {},
            {"id": 1},
            {"result": {}},
            {"id": 1, "result": {}, "error": {}},
            {"id": 1, "method": 42, "params": {}},
        )
        for message in invalid:
            with self.subTest(message=message):
                with self.assertRaises(CodexProtocolError):
                    classify_message(message)

    def test_response_validation_checks_id_error_and_object_result(self):
        self.assertEqual(response_result({"id": 2, "result": {"ok": True}}, 2), {"ok": True})
        for message in (
            {"id": 3, "result": {}},
            {"id": 2, "error": {"message": "no"}},
            {"id": 2, "result": "bad"},
        ):
            with self.subTest(message=message):
                with self.assertRaises(CodexProtocolError):
                    response_result(message, 2)


class CodexAppServerClientTests(unittest.TestCase):
    def test_full_initialize_thread_turn_and_completion_flow(self):
        requests = []

        def handler(process, message):
            requests.append(message)
            standard_handler(process, message)

        client, holder = make_client(handler)
        try:
            client.initialize()
            thread_id = client.start_thread()
            turn_id = client.start_turn(thread_id, "Inspect files")
            process = holder["process"]
            process.stdout.emit(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "delta": "not authoritative",
                    },
                }
            )
            process.stdout.emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "completedAtMs": 1,
                        "item": {
                            "id": "item-1",
                            "type": "agentMessage",
                            "text": "README.md",
                        },
                    },
                }
            )
            process.stdout.emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": turn_id,
                            "items": [],
                            "status": "completed",
                        },
                    },
                }
            )

            result = client.wait_for_turn(thread_id, turn_id)

            self.assertTrue(result.completed)
            self.assertEqual(result.final_message, "README.md")
            self.assertEqual(result.event_count, 3)
            self.assertEqual(
                [message["method"] for message in requests],
                ["initialize", "initialized", "thread/start", "turn/start"],
            )
            self.assertEqual(
                requests[0]["params"]["clientInfo"],
                {"name": "code-mule", "title": "Code Mule", "version": "0.1.0"},
            )
            self.assertEqual(
                requests[2]["params"],
                {
                    "cwd": "/tmp/project",
                    "approvalPolicy": "on-request",
                    "sandbox": "read-only",
                },
            )
            self.assertEqual(
                requests[3]["params"],
                {
                    "threadId": "thread-1",
                    "input": [{"type": "text", "text": "Inspect files"}],
                },
            )
            self.assertNotIn("title", requests[3]["params"])
            self.assertFalse(holder["kwargs"]["shell"])
            self.assertEqual(holder["kwargs"]["cwd"], "/tmp/project")
        finally:
            client.close()

    def test_supports_two_turns_on_one_thread(self):
        turn_number = 0

        def handler(process, message):
            nonlocal turn_number
            if message["method"] == "turn/start":
                turn_number += 1
                process.stdout.emit(
                    {
                        "id": message["id"],
                        "result": {"turn": {"id": f"turn-{turn_number}"}},
                    }
                )
                return
            standard_handler(process, message)

        client, holder = make_client(handler)
        try:
            client.initialize()
            thread_id = client.start_thread()
            for expected in ("turn-1", "turn-2"):
                turn_id = client.start_turn(thread_id, expected)
                self.assertEqual(turn_id, expected)
                holder["process"].stdout.emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": turn_id, "items": [], "status": "completed"},
                        },
                    }
                )
                self.assertTrue(client.wait_for_turn(thread_id, turn_id).completed)
        finally:
            client.close()

    def test_turn_start_passes_native_output_schema_unchanged(self):
        requests = []

        def handler(process, message):
            requests.append(message)
            standard_handler(process, message)

        client, _ = make_client(handler)
        try:
            client.initialize()
            thread_id = client.start_thread()
            schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            }
            client.start_turn(
                thread_id,
                "prompt",
                output_schema=schema,
            )
            self.assertEqual(requests[-1]["params"]["outputSchema"], schema)
        finally:
            client.close()

    def test_malformed_json_fails_closed(self):
        client, holder = make_client()
        client.start()
        holder["process"].stdout.emit("{bad json")
        with self.assertRaises(CodexProtocolError):
            client.initialize()
        client.close()

    def test_wrong_response_id_fails_closed(self):
        def handler(process, message):
            process.stdout.emit({"id": 999, "result": {}})

        client, _ = make_client(handler)
        with self.assertRaises(CodexProtocolError):
            client.initialize()
        client.close()

    def test_missing_thread_and_turn_ids_fail_closed(self):
        def missing_thread(process, message):
            if message["method"] == "initialize":
                standard_handler(process, message)
            elif message["method"] == "thread/start":
                process.stdout.emit({"id": message["id"], "result": {"thread": {}}})

        client, _ = make_client(missing_thread)
        client.initialize()
        with self.assertRaises(CodexProtocolError):
            client.start_thread()
        client.close()

        def missing_turn(process, message):
            if message["method"] == "turn/start":
                process.stdout.emit({"id": message["id"], "result": {"turn": {}}})
            else:
                standard_handler(process, message)

        client, _ = make_client(missing_turn)
        client.initialize()
        thread_id = client.start_thread()
        with self.assertRaises(CodexProtocolError):
            client.start_turn(thread_id, "prompt")
        client.close()

    def test_approval_and_user_input_requests_are_not_answered(self):
        cases = (
            ("item/commandExecution/requestApproval", CodexApprovalRequired),
            ("item/fileChange/requestApproval", CodexApprovalRequired),
            ("item/tool/requestUserInput", CodexUserInputRequired),
        )
        for method, expected_error in cases:
            with self.subTest(method=method):
                client, holder = make_client()
                client.initialize()
                thread_id = client.start_thread()
                turn_id = client.start_turn(thread_id, "prompt")
                holder["process"].stdout.emit(
                    {"id": 88, "method": method, "params": {"threadId": thread_id}}
                )
                with self.assertRaises(expected_error):
                    client.wait_for_turn(thread_id, turn_id)
                client.close()

    def test_explicit_failed_and_interrupted_turns_raise(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                client, holder = make_client()
                client.initialize()
                thread_id = client.start_thread()
                turn_id = client.start_turn(thread_id, "prompt")
                holder["process"].stdout.emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "items": [],
                                "status": status,
                                "error": {"message": "explicit failure"},
                            },
                        },
                    }
                )
                with self.assertRaisesRegex(CodexTurnFailed, "explicit failure"):
                    client.wait_for_turn(thread_id, turn_id)
                client.close()

    def test_non_retrying_error_notification_raises(self):
        client, holder = make_client()
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        holder["process"].stdout.emit(
            {
                "method": "error",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "willRetry": False,
                    "error": {"message": "model unavailable"},
                },
            }
        )
        with self.assertRaisesRegex(CodexTurnFailed, "model unavailable"):
            client.wait_for_turn(thread_id, turn_id)
        client.close()

    def test_timeout_terminates_child_without_retry(self):
        def silent(process, message):
            return None

        client, holder = make_client(silent, timeout=0.02)
        with self.assertRaises(CodexTurnTimeout):
            client.initialize()
        self.assertTrue(holder["process"].terminated)

    def test_child_exit_is_a_protocol_failure(self):
        def exiting(process, message):
            process.exit(7)

        client, _ = make_client(exiting)
        with self.assertRaisesRegex(CodexProtocolError, "exit 7"):
            client.initialize()
        client.close()


if __name__ == "__main__":
    unittest.main()
