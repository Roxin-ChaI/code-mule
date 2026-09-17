"""Manual real Codex Worker soak harness for Code Mule transport reliability.

This script is for the Boss to run by hand.  Codex must never execute it as
part of automated verification, because every iteration starts a real local
Codex app-server and a real bounded Worker turn.

It answers exactly one question: can Code Mule drive a real Codex Worker turn
to a trusted terminal result and a persisted ExecutionReport, repeatedly, with
zero unexplained transport loss?

Guarantees:

* one isolated disposable Git repository per iteration;
* no external network, no Computer Use, no MCP server, no approvals;
* a bounded task that must edit one file and run one local command;
* the ExecutionReport is persisted per iteration;
* a failing iteration keeps its workspace and forensic record, so evidence is
  never destroyed by cleanup (use ``--purge-failures`` to opt out).

Failure buckets follow the Worker failure classes: a rejected structured
report is a Code Mule runtime/report-contract failure, not a transport failure.

It contains no DeepSeek planning or review.

Usage:

    .venv/bin/python scripts/real_worker_soak.py
    .venv/bin/python scripts/real_worker_soak.py --iterations 10
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_mule.domain.enums import TaskStatus  # noqa: E402
from code_mule.domain.models import ExecutionReport, Task  # noqa: E402
from code_mule.transport import WorkerFailureClass  # noqa: E402
from code_mule.worker import (  # noqa: E402
    CodexWorkerConfig,
    CodexWorkerError,
    WorkerTaskRequest,
)
from code_mule.worker.service import CodexWorkerSession  # noqa: E402

EXPECTED_VALUE = "value = 42"
TASK_PROMPT = f"""\
Work inside this isolated disposable Git repository.

1. Read value.txt.
2. Change the deterministic value so value.txt contains exactly: {EXPECTED_VALUE}
3. Run this local verification command and record its result:
   python3 -c "import pathlib,sys;sys.exit(0 if '42' in pathlib.Path('value.txt').read_text() else 1)"
4. Return the required structured report.

Constraints:
- Do not use the network, browsers, MCP servers, or any external service.
- Do not create commits, tags, branches, or remote operations.
- files_changed must list value.txt.
"""


class SoakSetupError(RuntimeError):
    """Raised when the manual soak cannot start safely."""


@dataclass
class IterationResult:
    index: int
    passed: bool
    duration_seconds: float
    bucket: str
    failure_kind: str | None
    failure_class: str | None
    detail: str
    workspace: Path
    preserved: bool
    report_stage: str | None = None
    report_code: str | None = None
    report_field_path: str | None = None
    terminal_received: bool | None = None
    final_message_present: bool | None = None


def _run(argv: list[str], cwd: Path) -> None:
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SoakSetupError(
            f"{' '.join(argv)} failed with {completed.returncode}"
        )


def _prepare_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    _run(("git", "init", "-q"), workspace)
    _run(("git", "config", "user.email", "soak@code-mule.invalid"), workspace)
    _run(("git", "config", "user.name", "Code Mule Soak"), workspace)
    (workspace / "value.txt").write_text("value = 1\n", encoding="utf-8")
    _run(("git", "add", "value.txt"), workspace)
    _run(("git", "commit", "-q", "-m", "soak baseline"), workspace)
    return workspace


def _worker_config(workspace: Path) -> CodexWorkerConfig:
    return CodexWorkerConfig(
        command=("codex", "app-server"),
        workspace=workspace.resolve(),
        approval_policy="never",
        sandbox="workspace-write",
        inactivity_timeout_seconds=180,
        max_turn_seconds=900,
    )


def _task() -> Task:
    now = datetime.now(UTC)
    return Task(
        id="TASK-SOAK-001",
        milestone_id="MILESTONE-SOAK",
        title="Deterministic transport soak task",
        description="Edit value.txt and run one local verification command.",
        status=TaskStatus.IN_PROGRESS,
        dependencies=(),
        acceptance_criteria=(EXPECTED_VALUE,),
        execution_attempts=0,
        created_at=now,
        updated_at=now,
    )


def _classify(error: CodexWorkerError) -> tuple[str, str | None, str | None]:
    kind = error.transport_failure_kind
    failure_class = error.failure_class
    bucket = {
        WorkerFailureClass.CODEX_TURN_FAILURE: "codex_turn_failure",
        WorkerFailureClass.CODEX_PROCESS_FAILURE: "process_failure",
        WorkerFailureClass.TRANSPORT_FAILURE: "transport_failure",
        WorkerFailureClass.TIMEOUT: "timeout",
        WorkerFailureClass.USER_INTERRUPT: "user_interrupt",
        WorkerFailureClass.CODE_MULE_RUNTIME_FAILURE: "runtime_failure",
    }.get(failure_class)
    if bucket is None:
        bucket = "unknown_failure"
    return (
        bucket,
        None if kind is None else kind.value,
        None if failure_class is None else failure_class.value,
    )


def _verify_report(report: ExecutionReport, workspace: Path) -> str | None:
    """Return a failure detail, or None when the iteration is trustworthy."""

    content = (workspace / "value.txt").read_text(encoding="utf-8")
    if EXPECTED_VALUE not in content:
        return f"value.txt was not updated to {EXPECTED_VALUE!r}"
    if "value.txt" not in report.files_changed:
        return "ExecutionReport did not list value.txt in files_changed"
    if not report.tests:
        return "ExecutionReport recorded no local command result"
    if report.status != "completed":
        return f"ExecutionReport status was {report.status!r}"
    return None


def _write_forensics(
    path: Path,
    result: IterationResult,
    report: ExecutionReport | None,
    diagnostics,
) -> None:
    payload: dict[str, object] = {
        "index": result.index,
        "passed": result.passed,
        "bucket": result.bucket,
        "failure_kind": result.failure_kind,
        "failure_class": result.failure_class,
        "detail": result.detail,
        "duration_seconds": result.duration_seconds,
        "workspace": str(result.workspace),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if report is not None:
        payload["report"] = {
            "id": report.id,
            "status": report.status,
            "files_changed": list(report.files_changed),
            "tests": list(report.tests),
            "git_state": report.git_state,
        }
    if result.report_stage is not None or result.report_code is not None:
        payload["report_contract"] = {
            "stage": result.report_stage,
            "code": result.report_code,
            "field_path": result.report_field_path,
            "terminal_received": result.terminal_received,
            "final_message_present": result.final_message_present,
        }
    if diagnostics is not None:
        payload["transport"] = {
            "transport_failure_kind": (
                None
                if diagnostics.transport_failure_kind is None
                else diagnostics.transport_failure_kind.value
            ),
            "failure_class": (
                None
                if diagnostics.failure_class is None
                else diagnostics.failure_class.value
            ),
            "app_server_pid": diagnostics.app_server_pid,
            "app_server_exit_code": diagnostics.app_server_exit_code,
            "app_server_exit_signal": diagnostics.app_server_exit_signal,
            "stdout_state": diagnostics.stdout_state.value,
            "stderr_state": diagnostics.stderr_state.value,
            "stdin_state": diagnostics.stdin_state.value,
            "terminal_event_received": diagnostics.terminal_event_received,
            "terminal_event_type": diagnostics.terminal_event_type,
            "thread_id": diagnostics.thread_id,
            "turn_id": diagnostics.turn_id,
            "activity_count": diagnostics.activity_count,
            "process_alive_at_failure": diagnostics.process_alive_at_failure,
            "cleanup_reason": diagnostics.cleanup_reason,
            "reader_failure_kind": diagnostics.reader_failure_kind,
            "events": [
                {
                    "at": event.at.isoformat(),
                    "event_type": event.event_type,
                    "direction": event.direction.value,
                    "payload_category": event.payload_category,
                }
                for event in diagnostics.events
            ],
        }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def run_iteration(index: int, root: Path, artifacts: Path) -> IterationResult:
    workspace = _prepare_workspace(root)
    report: ExecutionReport | None = None
    started = time.monotonic()
    session = CodexWorkerSession(_worker_config(workspace))
    result: IterationResult
    try:
        session.start()
        report = session.execute(
            WorkerTaskRequest(_task(), TASK_PROMPT, "Deterministic soak task"),
            report_id=f"soak-report-{index}",
            created_at=datetime.now(UTC),
        )
        detail = _verify_report(report, workspace)
        passed = detail is None
        result = IterationResult(
            index=index,
            passed=passed,
            duration_seconds=time.monotonic() - started,
            bucket="passed" if passed else "incomplete_delivery",
            failure_kind=None,
            failure_class=None,
            detail=detail or "deterministic completion",
            workspace=workspace,
            preserved=not passed,
        )
    except CodexWorkerError as error:
        bucket, failure_kind, failure_class = _classify(error)
        terminal = getattr(error, "terminal", None)
        result = IterationResult(
            index=index,
            passed=False,
            duration_seconds=time.monotonic() - started,
            bucket=bucket,
            failure_kind=failure_kind,
            failure_class=failure_class,
            detail=type(error).__name__,
            workspace=workspace,
            preserved=True,
            report_stage=(
                None
                if getattr(error, "stage", None) is None
                else str(getattr(error.stage, "value", error.stage))
            ),
            report_code=(
                None
                if getattr(error, "code", None) is None
                else str(getattr(error.code, "value", error.code))
            ),
            report_field_path=getattr(error, "field_path", None),
            terminal_received=(
                None
                if terminal is None
                else bool(getattr(terminal, "turn_status", None) is not None)
            ),
            final_message_present=(
                None if terminal is None else bool(terminal.final_message_present)
            ),
        )
    except Exception as error:  # noqa: BLE001 - recorded, never swallowed
        result = IterationResult(
            index=index,
            passed=False,
            duration_seconds=time.monotonic() - started,
            bucket="runtime_failure",
            failure_kind=None,
            failure_class=None,
            detail=type(error).__name__,
            workspace=workspace,
            preserved=True,
        )
    finally:
        session.close()
        diagnostics = session.transport_diagnostics()
    _write_forensics(
        artifacts / f"iteration-{index:02d}.json", result, report, diagnostics
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="where to keep per-iteration records (default: a temp directory)",
    )
    parser.add_argument(
        "--purge-failures",
        action="store_true",
        help=(
            "delete failing workspaces too; by default a failing iteration keeps "
            "its workspace for forensics"
        ),
    )
    arguments = parser.parse_args(argv)
    if arguments.iterations < 1:
        raise SystemExit("--iterations must be at least 1")
    for tool in ("codex", "git"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is required for the real soak")

    run_root = Path(tempfile.mkdtemp(prefix="code-mule-soak-"))
    artifacts = arguments.artifacts_dir or run_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    print("REAL CODEX WORKER SOAK — MANUAL ONLY")
    print("This starts real local Codex app-server turns in disposable repositories.")
    print("No network, no MCP, no Computer Use, no DeepSeek planning is used.")
    print(f"run root   {run_root}")
    print(f"artifacts  {artifacts}")
    print("")

    results: list[IterationResult] = []
    for index in range(1, arguments.iterations + 1):
        root = run_root / f"iteration-{index:02d}"
        root.mkdir(parents=True)
        result = run_iteration(index, root, artifacts)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        suffix = "" if result.failure_kind is None else f" ({result.failure_kind})"
        if result.report_code is not None:
            suffix += (
                f" [{result.report_stage}/{result.report_code}"
                + (
                    ""
                    if result.report_field_path is None
                    else f" @ {result.report_field_path}"
                )
                + "]"
            )
        print(
            f"[{index:02d}/{arguments.iterations:02d}] {status} "
            f"{result.duration_seconds:6.1f}s  {result.bucket}{suffix}  "
            f"{result.detail}"
        )
        if result.passed:
            # Cleanup policy: a successful iteration is disposable, a failing
            # iteration keeps its workspace so evidence is never destroyed.
            shutil.rmtree(result.workspace, ignore_errors=True)
        elif arguments.purge_failures:
            shutil.rmtree(result.workspace, ignore_errors=True)

    durations = [item.duration_seconds for item in results]
    counts: dict[str, int] = {}
    for item in results:
        counts[item.bucket] = counts.get(item.bucket, 0) + 1

    passed = sum(1 for item in results if item.passed)
    unknown = counts.get("unknown_failure", 0)
    report_contract_failures = sum(
        1 for item in results if item.failure_kind == "report_parse_failed"
    )
    print("")
    print("RELIABILITY SUMMARY")
    print(f"Iterations           {len(results)}")
    print(f"Passed               {passed}")
    print(f"Codex turn failures  {counts.get('codex_turn_failure', 0)}")
    print(f"Process failures     {counts.get('process_failure', 0)}")
    print(f"Transport failures   {counts.get('transport_failure', 0)}")
    print(f"Timeouts             {counts.get('timeout', 0)}")
    print(f"Runtime failures     {counts.get('runtime_failure', 0)}")
    print(f"  of which report contract failures  {report_contract_failures}")
    print(f"User interrupts      {counts.get('user_interrupt', 0)}")
    print(f"Incomplete delivery  {counts.get('incomplete_delivery', 0)}")
    print(f"Unknown failures     {unknown}")
    print(f"Average duration     {sum(durations) / len(durations):.1f}s")
    print(f"Max duration         {max(durations):.1f}s")
    print("")
    if passed == len(results) and unknown == 0:
        print("P0 CODEX WORKER RELIABILITY PASS (manual real soak)")
        return 0
    if unknown != 0:
        print("P0 CODEX WORKER RELIABILITY FAIL — unknown failures must be 0")
    else:
        print(
            "P0 CODEX WORKER RELIABILITY FAIL — "
            f"{passed}/{len(results)} real iterations passed"
        )
    print("Residual workspaces and per-iteration records are preserved for forensics.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
