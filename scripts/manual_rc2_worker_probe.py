"""Boss-only, one-turn real Codex probe for the RC2 Worker boundary.

Run manually: .venv/bin/python scripts/manual_rc2_worker_probe.py --execute-real-worker
This starts an authenticated Codex app-server turn. It never calls DeepSeek.
The disposable Git workspace and safe diagnostics are preserved on every exit.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_mule.domain.enums import TaskStatus  # noqa: E402
from code_mule.domain.models import Task  # noqa: E402
from code_mule.worker import (  # noqa: E402
    CodexTurnFailed, CodexTurnTimeout, CodexWorkerConfig, CodexWorkerError,
    WorkerTaskRequest,
)
from code_mule.worker.contracts import SAFE_TURN_ERROR_CODES  # noqa: E402
from code_mule.worker.service import CodexWorkerSession  # noqa: E402


SERVICE_SOURCE = '''\
"""Minimal standard-library HTTP service; the probe must never launch it."""
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = b"RC2 probe"
            status = 200
        elif self.path == "/health":
            body = b"ok"
            status = 200
        else:
            body = b"not found"
            status = 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)


def main(host="127.0.0.1", port=8000):
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
'''

TASK_PROMPT = """\
Work only in this disposable Git repository. Read server.py and write
CONTRACT.md documenting its entry point, default host/port, and all GET routes
with success/error responses. Cite server.py line numbers. Do not change
server.py. Run only sandbox-safe static checks such as py_compile; do not
start the service or bind a socket. Do not use network, browsers, Computer Use,
MCP tools, external services, or sandbox escalation. Do not create Git commits.
Return the required structured Worker report; list CONTRACT.md as changed.
"""

_CONFIG = {
    "command": ["codex", "app-server"],
    "approval_policy": "on-request",
    "sandbox": "workspace-write",
    "inactivity_timeout_seconds": 120,
    "max_turn_seconds": 900,
    "model": "inherited_from_codex_config",
    "mcp": "inherited_from_codex_config",
}


class ProbeSetupError(RuntimeError):
    """A local fixture operation failed; never include raw command output."""


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args), cwd=workspace, capture_output=True, text=True,
        check=False, timeout=20,
    )
    if completed.returncode != 0:
        raise ProbeSetupError(f"git {args[0]} failed")
    return completed.stdout.strip()


def prepare_workspace(root: Path) -> tuple[Path, str]:
    workspace = root / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "Code Mule Probe")
    _git(workspace, "config", "user.email", "probe@example.invalid")
    (workspace / "server.py").write_text(SERVICE_SOURCE, encoding="utf-8")
    _git(workspace, "add", "--", "server.py")
    _git(workspace, "commit", "-q", "-m", "chore: initialize rc2 probe")
    if _git(workspace, "status", "--short"):
        raise ProbeSetupError("initial Git baseline is not clean")
    return workspace, _git(workspace, "rev-parse", "HEAD")


def _task() -> Task:
    now = datetime.now(UTC)
    return Task(
        id="task-1", milestone_id="milestone-1",
        title="Document the existing stdlib HTTP service contract",
        description="Read-only source discovery; write a contract document.",
        status=TaskStatus.IN_PROGRESS, dependencies=(),
        acceptance_criteria=("CONTRACT.md documents entry point, routes, and defaults",),
        execution_attempts=0, created_at=now, updated_at=now,
    )


def worker_config(workspace: Path) -> CodexWorkerConfig:
    return CodexWorkerConfig(
        command=("codex", "app-server"), workspace=workspace,
        approval_policy="on-request", sandbox="workspace-write",
        inactivity_timeout_seconds=120, max_turn_seconds=900,
    )


def _bounded_count(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else 0


def _safe_code(value: object) -> str | None:
    return value if isinstance(value, str) and value in SAFE_TURN_ERROR_CODES else None


def _safe_error_type(error: BaseException | None) -> str | None:
    if error is None:
        return None
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "UnknownError"


def _safe_timestamp(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) and value.tzinfo else None


def _diagnostics(session, error: BaseException | None, elapsed: float) -> dict[str, object]:
    transport = session.transport_diagnostics()
    retry_count = _bounded_count(getattr(session, "retryable_error_count", 0))
    mcp_count = _bounded_count(getattr(session, "mcp_startup_error_count", 0))
    details = error.details if isinstance(error, CodexTurnFailed) else None
    failure_kind = (
        None if not isinstance(error, CodexWorkerError)
        or error.transport_failure_kind is None
        else error.transport_failure_kind.value
    )
    timeout_kind = error.timeout_kind if isinstance(error, CodexTurnTimeout) else None
    events = [] if transport is None else [
        {
            "at": event.at.isoformat(),
            "event_type": event.event_type,
            "payload_category": event.payload_category,
        }
        for event in transport.events
    ]
    return {
        "error_type": _safe_error_type(error),
        "failure_kind": failure_kind,
        "turn_failure_kind": None if details is None else details.kind.value,
        "error_code": None if details is None else _safe_code(details.error_code),
        "retryable_error_count": retry_count,
        "first_retryable_error_at": _safe_timestamp(
            getattr(session, "first_retryable_error_at", None)
        ),
        "last_retryable_error_at": _safe_timestamp(
            getattr(session, "last_retryable_error_at", None)
        ),
        "last_retryable_error_code": _safe_code(
            getattr(session, "last_retryable_error_code", None)
        ),
        "mcp_startup_error_count": mcp_count,
        "first_mcp_startup_error_at": _safe_timestamp(
            getattr(session, "first_mcp_startup_error_at", None)
        ),
        "last_mcp_startup_error_at": _safe_timestamp(
            getattr(session, "last_mcp_startup_error_at", None)
        ),
        "upstream_error_count": retry_count + int(isinstance(error, CodexTurnFailed)),
        "local_timeout_count": int(isinstance(error, CodexTurnTimeout)),
        "timeout_kind": timeout_kind,
        "activity_count": 0 if transport is None else transport.activity_count,
        "last_valid_activity_at": (
            None if transport is None or transport.last_activity_at is None
            else transport.last_activity_at.isoformat()
        ),
        "terminal_event_received": (
            False if transport is None else transport.terminal_event_received
        ),
        "terminal_event_type": (
            None if transport is None else transport.terminal_event_type
        ),
        "cleanup_reason": None if transport is None else transport.cleanup_reason,
        "elapsed_seconds": round(max(0.0, elapsed), 3),
        "events": events,
    }


def run_probe(root: Path, *, session_factory=CodexWorkerSession) -> int:
    workspace, baseline_head = prepare_workspace(root)
    session = session_factory(worker_config(workspace))
    report = None
    error: BaseException | None = None
    started = time.monotonic()
    try:
        session.start()
        report = session.execute(
            WorkerTaskRequest(_task(), TASK_PROMPT, "RC2 Worker probe"),
            report_id="rc2-probe-report", created_at=datetime.now(UTC),
        )
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
    finally:
        session.close()

    status = _git(workspace, "status", "--short")
    head_unchanged = _git(workspace, "rev-parse", "HEAD") == baseline_head
    source_unchanged = (workspace / "server.py").read_text(encoding="utf-8") == SERVICE_SOURCE
    contract_present = (workspace / "CONTRACT.md").is_file()
    report_ok = (
        report is not None and report.status == "completed"
        and "CONTRACT.md" in report.files_changed
    )
    passed = error is None and report_ok and source_unchanged and contract_present and head_unchanged
    outcome = (
        "passed" if passed else "interrupted" if isinstance(error, KeyboardInterrupt)
        else "worker_failed" if isinstance(error, CodexWorkerError)
        else "runtime_failed" if error is not None else "verification_failed"
    )
    payload = {
        "probe": "rc2_worker_single_turn",
        "recorded_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "config": _CONFIG,
        "outcome": outcome,
        "git": {
            "baseline_head": baseline_head,
            "head_unchanged": head_unchanged,
            "source_unchanged": source_unchanged,
            "contract_present": contract_present,
            "workspace_dirty": bool(status),
        },
        "report_accepted": report_ok,
        "diagnostics": _diagnostics(session, error, time.monotonic() - started),
    }
    artifact = root / "probe-diagnostics.json"
    artifact.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Probe: {outcome}")
    print(f"Workspace preserved: {workspace}")
    print(f"Safe diagnostics: {artifact}")
    return 0 if passed else 130 if isinstance(error, KeyboardInterrupt) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-real-worker", action="store_true",
        help="explicitly authorize one locally authenticated Codex Worker turn",
    )
    args = parser.parse_args(argv)
    if not args.execute_real_worker:
        parser.error("manual real Codex execution requires --execute-real-worker")
    if shutil.which("codex") is None:
        print("Codex CLI is unavailable; no Worker started", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="code-mule-rc2-worker-probe-"))
    print("MANUAL REAL CODEX PROBE — one turn; no DeepSeek")
    print(f"Preserved probe root: {root}")
    try:
        return run_probe(root)
    except (ProbeSetupError, subprocess.TimeoutExpired, OSError) as error:
        print(f"Probe setup failed safely: {_safe_error_type(error)}", file=sys.stderr)
        print(f"Preserved probe root: {root}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
