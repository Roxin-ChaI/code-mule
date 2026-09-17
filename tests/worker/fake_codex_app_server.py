"""Deterministic fake Codex app-server for Worker transport reliability tests.

This fixture speaks the same JSONL stdio envelope as ``codex app-server`` and
is driven only by a scenario name.  It never calls a model, never touches the
network, and never reads repository content.  It exists so transport
supervision, loopback classification, and cleanup ordering can be exercised
against a real child process with real pipes, without a real Codex turn.

Run directly:  python fake_codex_app_server.py <scenario>
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

THREAD_ID = "thread-fake-1"
TURN_ID = "turn-fake-1"

VALID_REPORT = json.dumps(
    {
        "status": "completed",
        "summary": "fake worker completed deterministically",
        "files_changed": ["value.txt"],
        "tests": [
            {
                "name": "python verify",
                "status": "pass",
                "detail": None,
                "required": True,
            }
        ],
        "static_checks": [],
        "git_state": "dirty",
        "issues": [],
        "human_action": None,
    }
)

SCENARIOS = (
    "normal_completion",
    "turn_failed",
    "error_notification",
    "request_rejected",
    "stdout_eof_exit_zero",
    "stdout_eof_exit_nonzero",
    "stdout_eof_while_alive",
    "stderr_burst",
    "stdout_reader_exception",
    "malformed_jsonrpc",
    "delayed_terminal",
    "terminal_then_report_parse_failure",
    "inactivity_timeout",
    "hard_timeout",
    "clean_shutdown",
    "unrelated_notification",
    "duplicate_terminal",
    "late_event_from_old_turn",
    "signal_termination",
    "no_terminal_activity",
    "oversized_line",
    "soak_edit",
    "turn_failed_method",
)


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _send_raw(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _respond(request_id: int, result: dict) -> None:
    _send({"id": request_id, "result": result})


def _note(method: str, **params) -> None:
    _send(
        {
            "method": method,
            "params": {"threadId": THREAD_ID, "turnId": TURN_ID, **params},
        }
    )


def _activity() -> None:
    _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
    _note("item/started", item={"type": "commandExecution", "id": "cmd-1"})
    _note("item/completed", item={"type": "commandExecution", "id": "cmd-1"})


def _report(text: str = VALID_REPORT) -> None:
    _note(
        "item/completed",
        item={"type": "agentMessage", "id": "msg-1", "text": text},
    )


def _terminal(status: str = "completed") -> None:
    _send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": status, "items": []},
            },
        }
    )


def _completed_turn() -> None:
    _activity()
    _report()
    _terminal("completed")


def _close_stdout() -> None:
    try:
        sys.stdout.flush()
        os.close(1)
    except OSError:
        pass


def _incomplete_after_activity(scenario: str) -> None:
    if scenario == "normal_completion":
        _completed_turn()
    elif scenario == "soak_edit":
        # Deterministic stand-in for the manual real soak task: edit the one
        # tracked file, then report it.  Used only to self-check the harness.
        with open("value.txt", "w", encoding="utf-8") as handle:
            handle.write("value = 42\n")
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        _note(
            "item/started",
            item={
                "type": "fileChange",
                "id": "file-1",
                "changes": [{"path": "value.txt"}],
            },
        )
        _note("item/completed", item={"type": "fileChange", "id": "file-1"})
        _report()
        _terminal("completed")
    elif scenario == "turn_failed":
        _activity()
        _send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": THREAD_ID,
                    "turn": {
                        "id": TURN_ID,
                        "status": "failed",
                        "items": [],
                        "error": {"message": "fake failure", "code": "internal_error"},
                    },
                },
            }
        )
    elif scenario == "turn_failed_method":
        _activity()
        _send(
            {
                "method": "turn/failed",
                "params": {
                    "threadId": THREAD_ID,
                    "turnId": TURN_ID,
                    "turn": {"id": TURN_ID, "status": "failed"},
                },
            }
        )
    elif scenario == "error_notification":
        _activity()
        _note("error", willRetry=False, error={"message": "fake failure"})
    elif scenario == "stdout_eof_exit_zero":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        _close_stdout()
        os._exit(0)
    elif scenario == "stdout_eof_exit_nonzero":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        _close_stdout()
        os._exit(7)
    elif scenario == "stdout_eof_while_alive":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        _close_stdout()
        time.sleep(60)
    elif scenario == "stderr_burst":
        payload = ("x" * 200 + "\n").encode("utf-8")
        for _ in range(4_000):
            sys.stderr.buffer.write(payload)
        sys.stderr.buffer.flush()
        _completed_turn()
    elif scenario == "stdout_reader_exception":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        sys.stdout.flush()
        # Invalid UTF-8 decodes as a hard failure inside the text-mode reader.
        os.write(1, b"\xff\xfe\x00bad-utf8\n")
        time.sleep(60)
    elif scenario == "malformed_jsonrpc":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        _send_raw("{this is not json")
        time.sleep(60)
    elif scenario == "delayed_terminal":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        time.sleep(0.4)
        _note("item/started", item={"type": "fileChange", "id": "file-1"})
        time.sleep(0.4)
        _report()
        _terminal("completed")
    elif scenario == "terminal_then_report_parse_failure":
        _activity()
        _report("not a structured worker report")
        _terminal("completed")
    elif scenario == "no_terminal_activity":
        _activity()
        time.sleep(60)
    elif scenario == "inactivity_timeout":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        time.sleep(120)
    elif scenario == "hard_timeout":
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            _note("item/started", item={"type": "commandExecution", "id": "cmd-x"})
            time.sleep(0.05)
    elif scenario == "unrelated_notification":
        _note(
            "item/started",
            item={"type": "commandExecution", "id": "other"},
            threadId="thread-other",
        )
        _send({"method": "unknown/future", "params": {}})
        _send({"method": "unknown/no-params"})
        _note("turn/started", turn={"id": "turn-other", "status": "inProgress"})
        _send({"method": "account/updated", "params": {"signedIn": True}})
        _completed_turn()
    elif scenario == "duplicate_terminal":
        _completed_turn()
        _terminal("completed")
        time.sleep(60)
    elif scenario == "late_event_from_old_turn":
        _note(
            "item/started",
            item={"type": "commandExecution", "id": "cmd-old"},
            turnId="turn-old",
        )
        _send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": THREAD_ID,
                    "turn": {"id": "turn-old", "status": "failed"},
                },
            }
        )
        _completed_turn()
    elif scenario == "signal_termination":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    elif scenario == "oversized_line":
        _note("turn/started", turn={"id": TURN_ID, "status": "inProgress"})
        _send_raw(json.dumps({"method": "huge", "params": {"pad": "y" * 2_000_000}}))
        time.sleep(60)
    else:
        _completed_turn()


def main(argv: list[str]) -> int:
    scenario = argv[1] if len(argv) > 1 else "normal_completion"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _respond(request_id, {"userAgent": "fake-codex-app-server"})
        elif method == "thread/start":
            _respond(request_id, {"thread": {"id": THREAD_ID}})
        elif method == "turn/start":
            if scenario == "request_rejected":
                _send(
                    {
                        "id": request_id,
                        "error": {"code": -32602, "message": "invalid turn request"},
                    }
                )
                continue
            _respond(request_id, {"turn": {"id": TURN_ID, "status": "inProgress"}})
            _incomplete_after_activity(scenario)
        elif method == "initialized":
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
