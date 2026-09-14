import io
import json
import os
import queue
import subprocess
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from code_mule import __version__
from code_mule.progress import ProgressEventType, RecordingProgressSink
from code_mule.worker.client import CodexAppServerClient
from code_mule.worker.contracts import (
    CapabilityApprovalAction,
    CapabilityApprovalDecision,
    CodexCapabilityApprovalRequired,
    CodexProtocolError,
    CodexTurnFailed,
    CodexTurnHardTimeout,
    CodexTurnInactivityTimeout,
    CodexTurnTimeout,
    CodexUserInputRequired,
    CodexWorkerConfig,
    WorkerInputRequest,
)
from code_mule.worker.protocol import (
    MessageKind,
    classify_message,
    notification_message,
    parse_capability_approval_request,
    parse_native_approval_request,
    parse_worker_input_request,
    request_message,
    response_result,
    server_response_message,
)
from code_mule.domain import CapabilityApprovalScope


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


class ScriptedMonotonic:
    def __init__(self, *values):
        self.values = list(values)
        self.last = values[-1] if values else 0.0

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


def config(timeout=0.2):
    return CodexWorkerConfig(
        command=("codex", "app-server"),
        workspace=Path("/tmp/project"),
        approval_policy="on-request",
        sandbox="read-only",
        inactivity_timeout_seconds=timeout,
        max_turn_seconds=timeout,
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


def make_client(handler=standard_handler, timeout=0.2, progress_sink=None):
    holder = {}

    def factory(*args, **kwargs):
        holder["args"] = args
        holder["kwargs"] = kwargs
        holder["process"] = FakeProcess(handler)
        return holder["process"]

    return (
        CodexAppServerClient(
            config(timeout),
            popen_factory=factory,
            progress_sink=progress_sink,
            clock=lambda: datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        ),
        holder,
    )


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

    def test_user_input_requests_are_bounded_safe_projections(self):
        tool_request = parse_worker_input_request(
            {
                "id": 88,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "thread-1",
                    "questions": [
                        {
                            "id": "framework",
                            "question": "Which framework should be used?",
                            "options": [
                                {"label": "Flask", "description": "Small"},
                                {"label": "FastAPI", "description": "Typed"},
                            ],
                        }
                    ],
                    "token": "must-not-be-projected",
                },
            }
        )
        self.assertEqual(
            tool_request,
            WorkerInputRequest(
                "item/tool/requestUserInput",
                "88",
                "Which framework should be used?",
                ("Flask", "FastAPI"),
            ),
        )
        self.assertNotIn("must-not-be-projected", repr(tool_request))

        elicitation = parse_worker_input_request(
            {
                "id": "mcp-1",
                "method": "mcpServer/elicitation/request",
                "params": {
                    "message": "Choose a deployment region",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "region": {"type": "string", "enum": ["eu", "us"]}
                        },
                    },
                    "credential": "must-not-be-projected",
                },
            }
        )
        self.assertEqual(elicitation.question, "Choose a deployment region")
        self.assertEqual(elicitation.choices, ("eu", "us"))
        self.assertNotIn("must-not-be-projected", repr(elicitation))

    def test_structured_mcp_permission_is_not_product_input(self):
        message = {
            "id": 88,
            "method": "mcpServer/elicitation/request",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "serverName": "cua_repl",
                "message": "Allow the browser capability?",
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "connector_id": "browser-use",
                    "connector_name": "Google Chrome",
                    "tool_name": "control_browser",
                    "persist": ["session", "always"],
                    "credential": "must-not-be-projected",
                },
            },
        }
        with self.assertRaisesRegex(CodexProtocolError, "not Worker input"):
            parse_worker_input_request(message)
        request = parse_capability_approval_request(message)
        self.assertEqual(request.protocol_request_id, 88)
        self.assertEqual(request.thread_id, "thread-1")
        self.assertEqual(request.turn_id, "turn-1")
        self.assertEqual(request.capability, "Computer Use")
        self.assertEqual(request.application, "Google Chrome")
        self.assertEqual(
            request.available_scopes,
            (
                CapabilityApprovalScope.ONCE,
                CapabilityApprovalScope.SESSION,
                CapabilityApprovalScope.ALWAYS,
            ),
        )
        self.assertNotIn("credential", repr(request))
        self.assertNotIn("must-not-be-projected", repr(request))

    def test_native_capability_response_preserves_request_id_and_scope(self):
        self.assertEqual(
            server_response_message(
                "approval-1",
                CapabilityApprovalDecision(
                    CapabilityApprovalAction.ACCEPT,
                    CapabilityApprovalScope.SESSION,
                ),
            ),
            {
                "id": "approval-1",
                "result": {"action": "accept", "content": {"persist": "session"}},
            },
        )
        self.assertEqual(
            server_response_message(
                7, CapabilityApprovalDecision(CapabilityApprovalAction.DECLINE)
            ),
            {"id": 7, "result": {"action": "decline", "content": None}},
        )

    def test_user_input_request_rejects_malformed_or_unbounded_params(self):
        invalid = (
            {"id": 1, "method": "item/tool/requestUserInput", "params": {}},
            {
                "id": 1,
                "method": "item/tool/requestUserInput",
                "params": {"questions": [{"question": "Q", "options": "bad"}]},
            },
            {
                "id": 1,
                "method": "mcpServer/elicitation/request",
                "params": {"message": "Q", "requestedSchema": "bad"},
            },
            {
                "id": 1,
                "method": "mcpServer/elicitation/request",
                "params": {"message": "x" * 2_001},
            },
        )
        for message in invalid:
            with self.subTest(message=message):
                with self.assertRaises(CodexProtocolError):
                    parse_worker_input_request(message)


class CodexAppServerClientTests(unittest.TestCase):
    @staticmethod
    def _activity(thread_id, turn_id, item_id):
        return {
            "method": "item/started",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "id": item_id,
                    "type": "commandExecution",
                    "commandActions": [{"type": "read"}],
                },
            },
        }

    @staticmethod
    def _completed(thread_id, turn_id):
        return {
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed"},
            },
        }

    def test_valid_activity_refreshes_idle_limit_beyond_old_fixed_deadline(self):
        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        client._config = CodexWorkerConfig(
            command=("codex", "app-server"),
            workspace=Path("/tmp/project"),
            approval_policy="on-request",
            sandbox="read-only",
            inactivity_timeout_seconds=120,
            max_turn_seconds=900,
        )
        times = [0]
        for value in range(60, 421, 60):
            times.extend((value, value))
        times.append(421)
        client._monotonic = ScriptedMonotonic(*times)
        for index in range(7):
            holder["process"].stdout.emit(
                self._activity(thread_id, turn_id, f"item-{index}")
            )
        holder["process"].stdout.emit(self._completed(thread_id, turn_id))
        result = client.wait_for_turn(thread_id, turn_id)
        self.assertTrue(result.completed)
        client.close()

    def test_no_activity_uses_inactivity_timeout_and_cleans_up(self):
        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        client._config = replace(
            client._config,
            inactivity_timeout_seconds=120,
            max_turn_seconds=900,
        )
        client._monotonic = ScriptedMonotonic(0, 121)
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)
        self.assertTrue(holder["process"].terminated)
        self.assertEqual(client._reader_threads, [])

    def test_continuous_activity_still_stops_at_hard_deadline(self):
        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        client._config = replace(
            client._config,
            inactivity_timeout_seconds=120,
            max_turn_seconds=300,
        )
        client._monotonic = ScriptedMonotonic(
            0, 60, 60, 120, 120, 180, 180, 240, 240, 300
        )
        for index in range(5):
            holder["process"].stdout.emit(
                self._activity(thread_id, turn_id, f"item-{index}")
            )
        with self.assertRaises(CodexTurnHardTimeout):
            client.wait_for_turn(thread_id, turn_id)
        self.assertTrue(holder["process"].terminated)

    def test_unrelated_activity_does_not_refresh_but_retryable_current_error_does(self):
        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        client._config = replace(
            client._config,
            inactivity_timeout_seconds=120,
            max_turn_seconds=900,
        )
        holder["process"].stdout.emit(
            self._activity("other-thread", turn_id, "unrelated")
        )
        client._monotonic = ScriptedMonotonic(0, 60, 121)
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)

        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        client._config = replace(
            client._config,
            inactivity_timeout_seconds=120,
            max_turn_seconds=900,
        )
        holder["process"].stdout.emit(
            {
                "method": "error",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "willRetry": True,
                    "error": {"message": "temporary"},
                },
            }
        )
        holder["process"].stdout.emit(self._completed(thread_id, turn_id))
        client._monotonic = ScriptedMonotonic(0, 100, 100, 121)
        self.assertTrue(client.wait_for_turn(thread_id, turn_id).completed)
        client.close()

    def test_wrong_turn_activity_does_not_refresh_inactivity_deadline(self):
        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        client._config = replace(
            client._config,
            inactivity_timeout_seconds=120,
            max_turn_seconds=900,
        )
        holder["process"].stdout.emit(
            self._activity(thread_id, "other-turn", "unrelated")
        )
        client._monotonic = ScriptedMonotonic(0, 60, 121)
        with self.assertRaises(CodexTurnInactivityTimeout):
            client.wait_for_turn(thread_id, turn_id)

    def test_malformed_trusted_activity_fails_closed(self):
        client, holder = make_client(timeout=0.2)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        holder["process"].stdout.emit(
            {
                "method": "item/started",
                "params": {"threadId": thread_id, "turnId": turn_id},
            }
        )
        with self.assertRaises(CodexProtocolError):
            client.wait_for_turn(thread_id, turn_id)
        client.close()

    def test_projects_only_schema_backed_safe_worker_activity(self):
        progress = RecordingProgressSink()
        client, holder = make_client(progress_sink=progress)
        try:
            client.initialize()
            thread_id = client.start_thread()
            turn_id = client.start_turn(thread_id, "prompt")
            process = holder["process"]
            events = (
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": turn_id, "status": "inProgress"},
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "startedAtMs": 1,
                        "item": {
                            "id": "command-1",
                            "type": "commandExecution",
                            "command": "curl -H 'Authorization: secret-value'",
                            "commandActions": [{"type": "unknown"}],
                            "cwd": "/tmp/project",
                            "status": "inProgress",
                        },
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "startedAtMs": 2,
                        "item": {
                            "id": "file-1",
                            "type": "fileChange",
                            "changes": [
                                {
                                    "path": "/tmp/project/src/module.py",
                                    "kind": "update",
                                    "diff": "private diff",
                                }
                            ],
                            "status": "inProgress",
                        },
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "startedAtMs": 3,
                        "item": {
                            "id": "file-2",
                            "type": "fileChange",
                            "changes": [
                                {
                                    "path": "/Users/private/.ssh/id_rsa",
                                    "kind": "update",
                                    "diff": "private diff",
                                }
                            ],
                            "status": "inProgress",
                        },
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "completedAtMs": 4,
                        "item": {
                            "id": "message-1",
                            "type": "agentMessage",
                            "text": '{"status":"completed"}',
                        },
                    },
                },
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
                },
            )
            for event in events:
                process.stdout.emit(event)
            client.wait_for_turn(thread_id, turn_id)

            self.assertTrue(
                all(
                    event.type is ProgressEventType.WORKER_ACTIVITY
                    for event in progress.events
                )
            )
            messages = tuple(event.message for event in progress.events)
            self.assertEqual(
                messages,
                (
                    "Codex turn started",
                    "Running command",
                    "Editing src/module.py",
                    "Updating file",
                    "Codex response completed",
                    "Codex turn completed",
                ),
            )
            projected = repr(progress.events)
            self.assertNotIn("secret-value", projected)
            self.assertNotIn("id_rsa", projected)
            self.assertNotIn("private diff", projected)
        finally:
            client.close()

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
                {"name": "code-mule", "title": "Code Mule", "version": __version__},
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

    def test_worker_process_environment_excludes_parent_credentials(self):
        environment = {
            "PATH": "/usr/bin",
            "CODEX_HOME": "/tmp/codex-home",
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "OPENAI_API_KEY": "openai-secret",
            "GITHUB_TOKEN": "github-secret",
            "AWS_ACCESS_KEY_ID": "aws-key-id",
            "SERVICE_PASSWORD": "password-secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/credentials.json",
        }
        with patch.dict(os.environ, environment, clear=True):
            client, holder = make_client()
            try:
                client.start()
                child = holder["kwargs"]["env"]
                self.assertEqual(child["PATH"], "/usr/bin")
                self.assertEqual(child["CODEX_HOME"], "/tmp/codex-home")
                self.assertNotIn("DEEPSEEK_API_KEY", child)
                self.assertNotIn("OPENAI_API_KEY", child)
                self.assertNotIn("GITHUB_TOKEN", child)
                self.assertNotIn("AWS_ACCESS_KEY_ID", child)
                self.assertNotIn("SERVICE_PASSWORD", child)
                self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", child)
                self.assertNotIn("deepseek-secret", repr(child))
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
            (
                "item/commandExecution/requestApproval",
                CodexCapabilityApprovalRequired,
                {"command": ["python3", "server.py"], "reason": "Local verification"},
            ),
            (
                "item/fileChange/requestApproval",
                CodexCapabilityApprovalRequired,
                {},
            ),
            (
                "item/tool/requestUserInput",
                CodexUserInputRequired,
                {"questions": [{"question": "Continue?", "options": []}]},
            ),
            (
                "mcpServer/elicitation/request",
                CodexUserInputRequired,
                {"message": "Provide a value"},
            ),
        )
        for method, expected_error, params in cases:
            with self.subTest(method=method):
                client, holder = make_client()
                client.initialize()
                thread_id = client.start_thread()
                turn_id = client.start_turn(thread_id, "prompt")
                holder["process"].stdout.emit(
                    {
                        "id": 88,
                        "method": method,
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            **params,
                        },
                    }
                )
                with self.assertRaises(expected_error) as raised:
                    client.wait_for_turn(thread_id, turn_id)
                if isinstance(raised.exception, CodexUserInputRequired):
                    self.assertEqual(raised.exception.request.method, method)
                    self.assertEqual(raised.exception.request.request_id, "88")
                if isinstance(raised.exception, CodexCapabilityApprovalRequired):
                    self.assertEqual(raised.exception.request.thread_id, thread_id)
                    self.assertEqual(raised.exception.request.turn_id, turn_id)
                    self.assertEqual(raised.exception.request.request_id, "88")
                    self.assertEqual(
                        raised.exception.request.available_scopes,
                        (CapabilityApprovalScope.ONCE,),
                    )
                client.close()

    def test_capability_request_without_live_handler_is_typed_approval(self):
        client, holder = make_client()
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        holder["process"].stdout.emit(
            {
                "id": "approval-1",
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "serverName": "cua_repl",
                    "message": "Allow browser control?",
                    "_meta": {
                        "codex_approval_kind": "mcp_tool_call",
                        "connector_id": "browser-use",
                        "connector_name": "Google Chrome",
                    },
                },
            }
        )
        with self.assertRaises(CodexCapabilityApprovalRequired) as raised:
            client.wait_for_turn(thread_id, turn_id)
        self.assertEqual(raised.exception.request.request_id, "approval-1")
        client.close()

    def test_native_sandbox_approval_is_typed_without_raw_payload(self):
        request = parse_native_approval_request(
            {
                "id": "sandbox-7",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": ["python3", "server.py"],
                    "reason": "Local runtime verification",
                    "cwd": "/tmp/target",
                    "raw": "API_KEY=must-not-survive",
                },
            }
        )
        self.assertEqual(request.request_id, "sandbox-7")
        self.assertEqual(request.capability, "Sandbox escalation")
        self.assertEqual(request.tool_name, "python3 server.py")
        self.assertEqual(request.application, "Local runtime verification")
        self.assertEqual(request.capability_id, "/tmp/target")
        self.assertNotIn("must-not-survive", repr(request))

        redacted = parse_native_approval_request(
            {
                "id": "sandbox-8",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "python3 server.py --token=must-not-survive",
                    "reason": "authorization=must-not-survive",
                },
            }
        )
        self.assertNotIn("must-not-survive", repr(redacted))
        self.assertIn("[REDACTED]", repr(redacted))

    def test_live_capability_accept_and_reject_use_same_native_request(self):
        for action in (
            CapabilityApprovalAction.ACCEPT,
            CapabilityApprovalAction.DECLINE,
        ):
            with self.subTest(action=action):
                responses = []

                def handler(process, message):
                    if "method" in message:
                        standard_handler(process, message)
                        return
                    responses.append(message)
                    process.stdout.emit(self._completed("thread-1", "turn-1"))

                holder = {}

                def factory(*args, **kwargs):
                    holder["process"] = FakeProcess(handler)
                    return holder["process"]

                client = CodexAppServerClient(
                    config(),
                    popen_factory=factory,
                    capability_approval_handler=lambda request: CapabilityApprovalDecision(
                        action
                    ),
                )
                client.initialize()
                thread_id = client.start_thread()
                turn_id = client.start_turn(thread_id, "prompt")
                holder["process"].stdout.emit(
                    {
                        "id": 91,
                        "method": "mcpServer/elicitation/request",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "serverName": "cua_repl",
                            "message": "Allow browser control?",
                            "_meta": {
                                "codex_approval_kind": "mcp_tool_call",
                                "connector_id": "browser-use",
                                "connector_name": "Google Chrome",
                            },
                        },
                    }
                )
                result = client.wait_for_turn(thread_id, turn_id)
                self.assertTrue(result.completed)
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0]["id"], 91)
                self.assertEqual(responses[0]["result"]["action"], action.value)
                client.close()

    def test_capability_request_identity_and_handler_result_fail_closed(self):
        handlers = (
            lambda request: "accept",
            lambda request: CapabilityApprovalDecision(
                CapabilityApprovalAction.ACCEPT,
                CapabilityApprovalScope.ALWAYS,
            ),
        )
        for handler in handlers:
            with self.subTest(handler=handler):
                client, holder = make_client()
                client._capability_approval_handler = handler
                client.initialize()
                thread_id = client.start_thread()
                turn_id = client.start_turn(thread_id, "prompt")
                holder["process"].stdout.emit(
                    {
                        "id": 92,
                        "method": "mcpServer/elicitation/request",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "serverName": "cua_repl",
                            "message": "Allow browser control?",
                            "_meta": {
                                "codex_approval_kind": "mcp_tool_call",
                                "connector_id": "browser-use",
                            },
                        },
                    }
                )
                with self.assertRaises(CodexProtocolError):
                    client.wait_for_turn(thread_id, turn_id)
                client.close()

        client, holder = make_client()
        client._capability_approval_handler = lambda request: CapabilityApprovalDecision(
            CapabilityApprovalAction.ACCEPT
        )
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        holder["process"].stdout.emit(
            {
                "id": 93,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": "different-thread",
                    "turnId": turn_id,
                    "serverName": "cua_repl",
                    "message": "Allow browser control?",
                    "_meta": {
                        "codex_approval_kind": "mcp_tool_call",
                        "connector_id": "browser-use",
                    },
                },
            }
        )
        with self.assertRaisesRegex(CodexProtocolError, "current thread"):
            client.wait_for_turn(thread_id, turn_id)
        client.close()

        def failing_handler(request):
            raise RuntimeError("credential=must-not-leak")

        client, holder = make_client()
        client._capability_approval_handler = failing_handler
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")
        holder["process"].stdout.emit(
            {
                "id": 94,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "serverName": "cua_repl",
                    "message": "Allow browser control?",
                    "_meta": {
                        "codex_approval_kind": "mcp_tool_call",
                        "connector_id": "browser-use",
                    },
                },
            }
        )
        with self.assertRaises(CodexProtocolError) as raised:
            client.wait_for_turn(thread_id, turn_id)
        self.assertNotIn("must-not-leak", str(raised.exception))
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
                with self.assertRaisesRegex(CodexTurnFailed, "Codex turn failed"):
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
        with self.assertRaisesRegex(CodexTurnFailed, "error_notification"):
            client.wait_for_turn(thread_id, turn_id)
        client.close()

    def test_timeout_terminates_child_without_retry(self):
        def silent(process, message):
            return None

        client, holder = make_client(silent, timeout=0.02)
        with self.assertRaises(CodexTurnTimeout):
            client.initialize()
        self.assertTrue(holder["process"].terminated)

    def test_turn_deadline_terminates_child_and_reader_threads(self):
        client, holder = make_client(timeout=0.02)
        client.initialize()
        thread_id = client.start_thread()
        turn_id = client.start_turn(thread_id, "prompt")

        with self.assertRaises(CodexTurnTimeout):
            client.wait_for_turn(thread_id, turn_id)

        self.assertTrue(holder["process"].terminated)
        self.assertEqual(client._reader_threads, [])

    def test_child_exit_is_a_protocol_failure(self):
        def exiting(process, message):
            process.exit(7)

        client, _ = make_client(exiting)
        with self.assertRaisesRegex(CodexProtocolError, "exit 7"):
            client.initialize()
        client.close()


if __name__ == "__main__":
    unittest.main()
