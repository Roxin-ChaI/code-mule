"""Synchronous stdio client for the local Codex app-server."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Protocol, cast

from code_mule import __version__
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)

from .contracts import (
    CapabilityApprovalDecision,
    CodexAppServerDisconnected,
    CodexAppServerStartError,
    CodexCapabilityApprovalRequired,
    CodexJsonRpcDecodeError,
    CodexParentInterrupted,
    CodexProtocolError,
    CodexRequestRejected,
    CodexStdinWriteFailed,
    CodexStdoutReaderFailed,
    CodexTurnFailed,
    CodexTurnFailureDetails,
    CodexTurnFailureKind,
    SAFE_TURN_ERROR_CODES,
    CodexTurnHardTimeout,
    CodexTurnInactivityTimeout,
    CodexTurnTimeout,
    CodexUserInputRequired,
    CodexWorkerConfig,
    CodexWorkerError,
    WorkerCapabilityApprovalRequest,
    WorkerTurnTerminal,
    WorkerTurnResult,
)
from .protocol import (
    APPROVAL_REQUEST_METHODS,
    INITIALIZED_METHOD,
    INITIALIZE_METHOD,
    ITEM_COMPLETED_METHOD,
    MessageKind,
    THREAD_START_METHOD,
    TURN_COMPLETED_METHOD,
    TURN_START_METHOD,
    USER_INPUT_REQUEST_METHODS,
    classify_message,
    is_capability_approval_request,
    notification_message,
    parse_capability_approval_request,
    parse_native_approval_request,
    parse_worker_input_request,
    request_message,
    server_response_message,
)
from code_mule.transport import (
    MAX_EVENT_RING,
    ChannelState,
    TransportDiagnostics,
    TransportDirection,
    TransportEventRecord,
    TransportFailureKind,
    TransportState,
    WorkerFailureClass,
    failure_class_for,
)


class _Process(Protocol):
    stdin: IO[str] | None
    stdout: IO[str] | None
    stderr: IO[str] | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


_PopenFactory = Callable[..., _Process]
_CapabilityApprovalHandler = Callable[
    [WorkerCapabilityApprovalRequest], CapabilityApprovalDecision
]
_EOF = object()
_STDERR_TAIL_LIMIT = 100
_DEFAULT_EOF_GRACE_SECONDS = 0.5
_EOF_GRACE_POLL_SECONDS = 0.02
_SAFE_JSONRPC_ERROR_CODE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")


def _safe_jsonrpc_error_code(value: object) -> str | None:
    """Retain only a short, structured JSON-RPC error code."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 <= value <= 1_000_000 else None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if 1 <= len(normalized) <= 64 and all(
            character in _SAFE_JSONRPC_ERROR_CODE for character in normalized
        ):
            return normalized
    return None


def _safe_method_name(method: object) -> str | None:
    """Normalize one protocol method name into a bounded event type."""

    if not isinstance(method, str) or method == "":
        return None
    normalized = method.lower().replace("/", ".")[:120]
    if not normalized[0].isascii() or not normalized[0].isalnum():
        return None
    if not all(
        character.isascii()
        and (character.isalnum() or character in "_./-")
        for character in normalized
    ):
        return None
    return normalized


_HANDLED_TURN_METHODS = frozenset(
    {
        "turn/started",
        "turn/completed",
        "item/started",
        "item/completed",
        "item/agentMessage/delta",
        "error",
    }
)

_ITEM_CATEGORY = {
    "agentMessage": "item.agent_message",
    "commandExecution": "item.command_execution",
    "fileChange": "item.file_change",
    "reasoning": "item.reasoning",
}


def _payload_category(params: dict[str, object]) -> str | None:
    """Describe *what kind* of payload arrived, never the payload itself."""

    if "error" in params:
        return "error"
    item = params.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if isinstance(item_type, str):
            return _ITEM_CATEGORY.get(item_type, "item.other")
        return "item.other"
    turn = params.get("turn")
    if isinstance(turn, dict):
        return "turn"
    return None


def _reader_failure_kind(error: BaseException) -> str:
    name = type(error).__name__.lower()
    normalized = "".join(
        character for character in name if character.isascii() and (
            character.isalnum() or character in "_."
        )
    )[:64]
    return normalized or "reader_failed"


def _worker_environment() -> dict[str, str]:
    """Keep the local runtime usable without exposing parent-process secrets."""

    secret_segments = {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "CREDENTIALS",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if secret_segments.isdisjoint(key.upper().split("_"))
    }


class CodexAppServerClient:
    """One local app-server process with one request in flight at a time."""

    def __init__(
        self,
        config: CodexWorkerConfig,
        *,
        popen_factory: _PopenFactory = subprocess.Popen,
        progress_sink: ProgressSink | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        capability_approval_handler: _CapabilityApprovalHandler | None = None,
        eof_grace_seconds: float = _DEFAULT_EOF_GRACE_SECONDS,
    ) -> None:
        self._config = config
        self._popen_factory = popen_factory
        self._process: _Process | None = None
        self._stdout_queue: queue.Queue[object] = queue.Queue()
        self._pending_messages: deque[dict[str, object]] = deque()
        self._stderr_lines: deque[str] = deque(maxlen=_STDERR_TAIL_LIMIT)
        self._reader_threads: list[threading.Thread] = []
        self._next_request_id = 1
        self._initialized = False
        self._progress = resilient_progress_sink(progress_sink)
        self._clock = clock
        self._monotonic = monotonic
        self._capability_approval_handler = capability_approval_handler
        self._eof_grace_seconds = max(0.0, float(eof_grace_seconds))
        self._closing = False
        self._state = TransportState.PROCESS_STARTING
        self._app_server_pid: int | None = None
        self._app_server_started_at: datetime | None = None
        self._app_server_command: str | None = None
        self._app_server_exit_code: int | None = None
        self._app_server_exit_signal: int | None = None
        self._stdout_state = ChannelState.UNOPENED
        self._stderr_state = ChannelState.UNOPENED
        self._stdin_state = ChannelState.UNOPENED
        self._events: deque[TransportEventRecord] = deque(maxlen=MAX_EVENT_RING)
        self._failure_kind: TransportFailureKind | None = None
        self._cleanup_reason: str | None = None
        self._reader_failure_kind: str | None = None
        self._process_alive_at_failure: bool | None = None
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._active_request_id: int | None = None
        self._terminal: WorkerTurnTerminal | None = None
        self._activity_count = 0
        self._last_activity_at: datetime | None = None
        self._retryable_error_count = 0
        self._mcp_startup_error_count = 0
        self._last_retryable_error_code: str | None = None
        self._first_retryable_error_at: datetime | None = None
        self._last_retryable_error_at: datetime | None = None
        self._first_mcp_startup_error_at: datetime | None = None
        self._last_mcp_startup_error_at: datetime | None = None
        self._last_protocol_event_type: str | None = None
        self._last_protocol_event_at: datetime | None = None

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr_lines)

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    @property
    def retryable_error_count(self) -> int:
        """Exact bounded count for this process; no provider message retained."""

        return self._retryable_error_count

    @property
    def mcp_startup_error_count(self) -> int:
        return self._mcp_startup_error_count

    @property
    def last_retryable_error_code(self) -> str | None:
        return self._last_retryable_error_code

    @property
    def first_retryable_error_at(self) -> datetime | None:
        return self._first_retryable_error_at

    @property
    def last_retryable_error_at(self) -> datetime | None:
        return self._last_retryable_error_at

    @property
    def first_mcp_startup_error_at(self) -> datetime | None:
        return self._first_mcp_startup_error_at

    @property
    def last_mcp_startup_error_at(self) -> datetime | None:
        return self._last_mcp_startup_error_at

    @property
    def transport_state(self) -> TransportState:
        return self._state

    @property
    def terminal_evidence(self) -> WorkerTurnTerminal | None:
        """Trusted terminal turn evidence captured before report parsing."""

        return self._terminal

    def mark_report_state(self, state: TransportState) -> None:
        """Advance the report half of the lifecycle after a trusted terminal.

        Only the ordered report stages are accepted, and the stage never
        regresses, so a failed extraction cannot look like a persisted report.
        """

        if state not in {
            TransportState.REPORT_EXTRACTION,
            TransportState.REPORT_PARSED,
            TransportState.REPORT_VALIDATED,
            TransportState.REPORT_PERSISTED,
        }:
            raise ValueError("report state must be a report lifecycle stage")
        order = (
            TransportState.REPORT_EXTRACTION,
            TransportState.REPORT_PARSED,
            TransportState.REPORT_VALIDATED,
            TransportState.REPORT_PERSISTED,
        )
        if self._state in order and order.index(state) <= order.index(self._state):
            return
        if self._state not in order and self._state is not TransportState.TURN_TERMINAL:
            # A report stage is only meaningful after a trusted terminal turn.
            return
        self._state = state

    def transport_diagnostics(self) -> TransportDiagnostics:
        """Bounded, secret-free supervision facts for this attempt."""

        return TransportDiagnostics(
            stdout_state=self._stdout_state,
            stderr_state=self._stderr_state,
            stdin_state=self._stdin_state,
            terminal_event_received=self._terminal is not None,
            activity_count=self._activity_count,
            report_lifecycle=(
                self._state
                if self._state
                in {
                    TransportState.REPORT_EXTRACTION,
                    TransportState.REPORT_PARSED,
                    TransportState.REPORT_VALIDATED,
                    TransportState.REPORT_PERSISTED,
                }
                else None
            ),
            app_server_pid=self._app_server_pid,
            app_server_started_at=self._app_server_started_at,
            app_server_command=self._app_server_command,
            app_server_exit_code=self._app_server_exit_code,
            app_server_exit_signal=self._app_server_exit_signal,
            transport_failure_kind=self._failure_kind,
            failure_class=failure_class_for(self._failure_kind),
            last_protocol_event_type=self._last_protocol_event_type,
            last_protocol_event_at=self._last_protocol_event_at,
            terminal_event_type=(
                None if self._terminal is None else self._terminal.terminal_event_type
            ),
            thread_id=self._thread_id,
            turn_id=self._turn_id,
            request_id=self._active_request_id,
            last_activity_at=self._last_activity_at,
            reader_failure_kind=self._reader_failure_kind,
            process_alive_at_failure=self._process_alive_at_failure,
            cleanup_reason=self._cleanup_reason,
            events=tuple(self._events),
        )

    def start(self) -> None:
        if self._process is not None:
            return
        self._state = TransportState.PROCESS_STARTING
        try:
            process = self._popen_factory(
                self._config.command,
                cwd=str(self._config.workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                shell=False,
                env=_worker_environment(),
            )
        except OSError as error:
            raise self._fail(
                TransportFailureKind.APP_SERVER_START_FAILED,
                "unable to start the local Codex app-server",
            ) from error
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.terminate()
            raise self._fail(
                TransportFailureKind.APP_SERVER_START_FAILED,
                "Codex app-server did not expose all required stdio pipes",
            )
        self._process = process
        pid = getattr(process, "pid", None)
        self._app_server_pid = pid if type(pid) is int and pid > 0 else None
        self._app_server_started_at = self._now()
        self._app_server_command = " ".join(self._config.command)[:240]
        self._stdout_state = ChannelState.OPEN
        self._stderr_state = ChannelState.OPEN
        self._stdin_state = ChannelState.OPEN
        self._state = TransportState.PROCESS_RUNNING
        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout,),
            name="code-mule-codex-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr,),
            name="code-mule-codex-stderr",
            daemon=True,
        )
        self._reader_threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()

    def _now(self) -> datetime:
        return self._clock() if self._clock is not None else datetime.now(UTC)

    def _record_event(
        self,
        event_type: str,
        direction: TransportDirection,
        *,
        correlation_id: str | None = None,
        payload_category: str | None = None,
    ) -> None:
        normalized = _safe_method_name(event_type)
        if normalized is None:
            return
        safe_correlation = (
            correlation_id
            if isinstance(correlation_id, str)
            and 0 < len(correlation_id) <= 128
            and all(
                character.isascii()
                and (character.isalnum() or character in "_-")
                for character in correlation_id
            )
            else None
        )
        now = self._now()
        self._last_protocol_event_type = normalized
        self._last_protocol_event_at = now
        self._events.append(
            TransportEventRecord(
                at=now,
                event_type=normalized,
                direction=direction,
                correlation_id=safe_correlation,
                payload_category=payload_category,
            )
        )

    def _fail(
        self, kind: TransportFailureKind, message: str
    ) -> CodexWorkerError:
        self._failure_kind = kind
        process = self._process
        self._process_alive_at_failure = (
            None if process is None else process.poll() is None
        )
        error: CodexWorkerError
        if kind is TransportFailureKind.APP_SERVER_START_FAILED:
            error = CodexAppServerStartError(message)
        elif kind is TransportFailureKind.STDIN_WRITE_FAILED:
            error = CodexStdinWriteFailed(message)
        elif kind is TransportFailureKind.STDOUT_READER_FAILED:
            error = CodexStdoutReaderFailed(message)
        elif kind is TransportFailureKind.JSONRPC_DECODE_FAILED:
            error = CodexJsonRpcDecodeError(message)
        elif kind is TransportFailureKind.PARENT_INTERRUPTED:
            error = CodexParentInterrupted(message)
        elif kind is TransportFailureKind.REQUEST_REJECTED:
            error = CodexRequestRejected(message)
        elif kind is TransportFailureKind.PROTOCOL_VIOLATION:
            error = CodexProtocolError(message)
        else:
            error = CodexAppServerDisconnected(message)
        error.transport_failure_kind = kind
        return error

    def initialize(self) -> None:
        self.start()
        result = self._request(
            INITIALIZE_METHOD,
            {
                "clientInfo": {
                    "name": "code-mule",
                    "title": "Code Mule",
                    "version": __version__,
                }
            },
        )
        if not isinstance(result, dict):
            raise self._fail(
                TransportFailureKind.PROTOCOL_VIOLATION,
                "initialize result must be an object",
            )
        self._write_message(notification_message(INITIALIZED_METHOD))
        self._initialized = True
        self._state = TransportState.CHANNEL_READY

    def start_thread(self) -> str:
        self._require_initialized()
        result = self._request(
            THREAD_START_METHOD,
            {
                "cwd": str(self._config.workspace),
                "approvalPolicy": self._config.approval_policy,
                "sandbox": self._config.sandbox,
            },
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise self._fail(
                TransportFailureKind.PROTOCOL_VIOLATION,
                "thread/start result is missing thread",
            )
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or thread_id == "":
            raise self._fail(
                TransportFailureKind.PROTOCOL_VIOLATION,
                "thread/start result is missing thread.id",
            )
        self._thread_id = thread_id
        self._state = TransportState.THREAD_CREATED
        return thread_id

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
    ) -> str:
        self._require_initialized()
        if thread_id == "":
            raise ValueError("thread_id must not be empty")
        if prompt == "":
            raise ValueError("prompt must not be empty")
        params: dict[str, object] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema
        result = self._request(TURN_START_METHOD, params)
        turn = result.get("turn")
        if not isinstance(turn, dict):
            raise self._fail(
                TransportFailureKind.PROTOCOL_VIOLATION,
                "turn/start result is missing turn",
            )
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or turn_id == "":
            raise self._fail(
                TransportFailureKind.PROTOCOL_VIOLATION,
                "turn/start result is missing turn.id",
            )
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._state = TransportState.TURN_STARTED
        return turn_id

    def wait_for_turn(self, thread_id: str, turn_id: str) -> WorkerTurnResult:
        if thread_id == "" or turn_id == "":
            raise ValueError("thread_id and turn_id must not be empty")
        started = self._monotonic()
        hard_deadline = started + self._config.max_turn_seconds
        inactivity_deadline = started + self._config.inactivity_timeout_seconds
        event_count = 0
        # The protocol classifies an assistant message as commentary or
        # final_answer, but explicitly warns that providers do not emit the
        # phase consistently.  Prefer the final answer, fall back to the last
        # observed agent message, and never let commentary text shadow a
        # declared final answer.
        final_answer_text: str | None = None
        last_agent_message_text: str | None = None
        final_message: str | None = None
        issues: list[str] = []
        activity_count = 0
        last_activity: float | None = None

        while True:
            try:
                message = self._next_turn_message(
                    hard_deadline, inactivity_deadline
                )
            except CodexTurnTimeout as error:
                self._failure_kind = error.transport_failure_kind
                self._process_alive_at_failure = (
                    None if self._process is None else self._process.poll() is None
                )
                self.close(reason="timeout")
                raise
            except CodexParentInterrupted:
                self.close(reason="parent_interrupted")
                raise
            except KeyboardInterrupt:
                # A Ctrl+C at the parent boundary is a typed stop cause, not an
                # unexplained transport loss.  The interrupt is re-raised so the
                # CLI keeps its normal signal semantics.
                self._failure_kind = TransportFailureKind.PARENT_INTERRUPTED
                self._process_alive_at_failure = (
                    None if self._process is None else self._process.poll() is None
                )
                self.close(reason="parent_interrupted")
                raise
            self._check_reader_health()
            kind = classify_message(message)
            if kind is MessageKind.RESPONSE:
                raise self._fail(
                    TransportFailureKind.PROTOCOL_VIOLATION,
                    "unexpected response while waiting for turn",
                )
            if kind is MessageKind.SERVER_REQUEST:
                event_count += 1
                if self._raise_server_request(
                    message,
                    expected_thread_id=thread_id,
                    expected_turn_id=turn_id,
                ):
                    continue

            method = message.get("method")
            if not isinstance(method, str) or method == "":
                raise self._fail(
                    TransportFailureKind.PROTOCOL_VIOLATION,
                    "app-server notification is missing its method",
                )
            params = self._tolerant_params(message)
            self._record_event(
                method,
                TransportDirection.FROM_WORKER,
                correlation_id=self._event_correlation(params, thread_id, turn_id),
                payload_category=(
                    "retryable_turn_error"
                    if method == "error"
                    and isinstance(params, dict)
                    and params.get("threadId") == thread_id
                    and params.get("turnId") == turn_id
                    and params.get("willRetry") is True
                    else _payload_category(params)
                ),
            )
            if (
                method == "mcpServer/startupStatus/updated"
                and isinstance(params, dict)
                and "error" in params
            ):
                observed_at = self._now()
                self._mcp_startup_error_count = min(
                    self._mcp_startup_error_count + 1, 1_000_000_000
                )
                if self._first_mcp_startup_error_at is None:
                    self._first_mcp_startup_error_at = observed_at
                self._last_mcp_startup_error_at = observed_at
            self._state = TransportState.TURN_ACTIVE
            if params is None:
                # An unrelated or malformed notification must not destroy the
                # live transport; only handled methods are held to the schema.
                if method in _HANDLED_TURN_METHODS:
                    raise self._fail(
                        TransportFailureKind.PROTOCOL_VIOLATION,
                        f"{method} params must be an object",
                    )
                continue
            matches_thread = params.get("threadId") == thread_id
            matches_turn = params.get("turnId") == turn_id
            turn = params.get("turn")
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                matches_turn = True

            if self._is_trusted_turn_activity(
                method, params, thread_id, turn_id
            ):
                last_activity = self._monotonic()
                activity_count += 1
                self._activity_count = activity_count
                self._last_activity_at = self._now()
                inactivity_deadline = last_activity + self._config.inactivity_timeout_seconds

            if matches_thread and matches_turn:
                self._project_activity(method, params)

            if method == ITEM_COMPLETED_METHOD:
                if matches_thread and matches_turn:
                    self._require_event_identity(params, method)
                    event_count += 1
                    item = params.get("item")
                    if not isinstance(item, dict):
                        raise self._fail(
                            TransportFailureKind.PROTOCOL_VIOLATION,
                            "item/completed is missing item",
                        )
                    if item.get("type") == "agentMessage":
                        text = item.get("text")
                        if not isinstance(text, str):
                            raise self._fail(
                                TransportFailureKind.PROTOCOL_VIOLATION,
                                "completed agentMessage is missing text",
                            )
                        phase = item.get("phase")
                        if phase is not None and phase not in {
                            "commentary",
                            "final_answer",
                        }:
                            raise self._fail(
                                TransportFailureKind.PROTOCOL_VIOLATION,
                                "completed agentMessage has an unknown phase",
                            )
                        if phase == "final_answer":
                            final_answer_text = text
                        elif phase == "commentary":
                            # Mid-turn narration is never the report.
                            pass
                        else:
                            last_agent_message_text = text
                        final_message = final_answer_text or last_agent_message_text
                continue

            if method == "error":
                if matches_thread and matches_turn:
                    self._require_event_identity(params, method)
                    event_count += 1
                    error_code = self._safe_failure_code(
                        params.get("error"), allow_none=False
                    )
                    if "willRetry" in params and type(params["willRetry"]) is not bool:
                        raise self._fail(
                            TransportFailureKind.PROTOCOL_VIOLATION,
                            "error notification has invalid willRetry",
                        )
                    if params.get("willRetry") is not True:
                        raise self._turn_failure(
                            CodexTurnFailureKind.ERROR_NOTIFICATION, thread_id, turn_id,
                            params.get("error"), None, params.get("willRetry"),
                            activity_count, last_activity, started,
                        )
                    self._retryable_error_count = min(
                        self._retryable_error_count + 1, 1_000_000_000
                    )
                    self._last_retryable_error_code = error_code
                    observed_at = self._now()
                    if self._first_retryable_error_at is None:
                        self._first_retryable_error_at = observed_at
                    self._last_retryable_error_at = observed_at
                    retry_notice = "Codex reported a retryable turn error"
                    if retry_notice not in issues:
                        issues.append(retry_notice)
                continue

            if method == TURN_COMPLETED_METHOD:
                if matches_thread and matches_turn:
                    if not isinstance(turn, dict) or not isinstance(
                        turn.get("id"), str
                    ):
                        raise self._fail(
                            TransportFailureKind.PROTOCOL_VIOLATION,
                            "turn/completed is missing turn.id",
                        )
                    if not isinstance(params.get("threadId"), str):
                        raise self._fail(
                            TransportFailureKind.PROTOCOL_VIOLATION,
                            "turn/completed is missing threadId",
                        )
                    event_count += 1
                    status = turn.get("status")
                    if status == "completed":
                        # Persist trusted terminal evidence *before* any report
                        # parsing so a later parser failure can never look like
                        # a missing terminal result.
                        self._terminal = WorkerTurnTerminal(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            terminal_event_type="turn/completed",
                            turn_status="completed",
                            activity_count=activity_count,
                            event_count=event_count,
                            final_message_present=final_message is not None,
                        )
                        self._state = TransportState.TURN_TERMINAL
                        self._record_event(
                            "turn/completed",
                            TransportDirection.FROM_WORKER,
                            correlation_id=turn_id,
                            payload_category="terminal.completed",
                        )
                        return WorkerTurnResult(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            final_message=final_message,
                            completed=True,
                            event_count=event_count,
                            issues=tuple(issues),
                        )
                    if status in {"failed", "interrupted"}:
                        self._terminal = WorkerTurnTerminal(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            terminal_event_type="turn/completed",
                            turn_status=status,
                            activity_count=activity_count,
                            event_count=event_count,
                            final_message_present=final_message is not None,
                        )
                        self._state = TransportState.TURN_TERMINAL
                        raise self._turn_failure(
                            CodexTurnFailureKind.TURN_FAILED if status == "failed"
                            else CodexTurnFailureKind.TURN_INTERRUPTED,
                            thread_id, turn_id, turn.get("error"), status, None,
                            activity_count, last_activity, started,
                        )
                    raise self._fail(
                        TransportFailureKind.PROTOCOL_VIOLATION,
                        f"turn/completed has invalid status {status!r}",
                    )
                continue

            if matches_thread and matches_turn:
                event_count += 1

    @staticmethod
    def _safe_failure_code(error: object, *, allow_none: bool = True) -> str | None:
        if error is None and allow_none:
            return None
        if not isinstance(error, dict):
            raise CodexProtocolError("turn error must be an object")
        if "message" in error and not isinstance(error["message"], str):
            raise CodexProtocolError("turn error message has invalid type")
        code = error.get("code")
        return code if isinstance(code, str) and code in SAFE_TURN_ERROR_CODES else None

    @staticmethod
    def _tolerant_params(message: dict[str, object]) -> dict[str, object] | None:
        """Return params, or None when an unrelated notification is malformed."""

        params = message.get("params")
        if params is None:
            return {}
        if not isinstance(params, dict) or not all(
            isinstance(key, str) for key in params
        ):
            return None
        return cast(dict[str, object], params)

    @staticmethod
    def _event_correlation(
        params: dict[str, object], thread_id: str, turn_id: str
    ) -> str | None:
        """Correlate one ring-buffer entry without retaining payload content."""

        for value in (params.get("turnId"), params.get("threadId")):
            if isinstance(value, str) and value in {thread_id, turn_id}:
                return value
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("id") in {thread_id, turn_id}:
            return cast(str, turn["id"])
        return None

    def _check_reader_health(self) -> None:
        """Propagate a reader-thread failure instead of losing it silently."""

        if self._stderr_state is ChannelState.FAILED and self._failure_kind is None:
            raise self._fail(
                TransportFailureKind.STDERR_READER_FAILED,
                "Codex app-server stderr reader failed",
            )

    def _turn_failure(
        self, kind, thread_id, turn_id, error, status, will_retry,
        activity_count, last_activity, started,
    ) -> CodexTurnFailed:
        code = self._safe_failure_code(error)
        failed_at = self._monotonic()
        try:
            details = CodexTurnFailureDetails(
                kind=kind, thread_id=thread_id, turn_id=turn_id,
                turn_status=status, will_retry=will_retry, error_code=code,
                activity_count=activity_count,
                last_activity_age_seconds=(None if last_activity is None else failed_at - last_activity),
                turn_elapsed_seconds=failed_at - started,
            )
        except (TypeError, ValueError):
            raise CodexProtocolError("turn failure diagnostics are malformed") from None
        self._failure_kind = {
            CodexTurnFailureKind.ERROR_NOTIFICATION: TransportFailureKind.ERROR_NOTIFICATION,
            CodexTurnFailureKind.TURN_FAILED: TransportFailureKind.TURN_FAILED,
            CodexTurnFailureKind.TURN_INTERRUPTED: TransportFailureKind.TURN_INTERRUPTED,
        }[kind]
        self._process_alive_at_failure = (
            None if self._process is None else self._process.poll() is None
        )
        return CodexTurnFailed(details=details)

    def _project_activity(
        self, method: str, params: dict[str, object]
    ) -> None:
        if self._clock is None:
            return
        message: str | None = None
        metadata: dict[str, str] = {}
        if method == "turn/started":
            message = "Codex turn started"
            metadata = {"activity": "turn.started"}
        elif method == "turn/completed":
            message = "Codex turn completed"
            metadata = {"activity": "turn.completed"}
        elif method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                return
            item_type = item.get("type")
            phase = "started" if method == "item/started" else "completed"
            if item_type == "commandExecution":
                message = (
                    self._safe_command_message(item)
                    if phase == "started"
                    else "Command completed"
                )
                metadata = {"activity": f"command_execution.{phase}"}
            elif item_type == "fileChange":
                safe_path = self._safe_file_path(item)
                message = (
                    f"Editing {safe_path}"
                    if safe_path is not None and phase == "started"
                    else (
                        f"Updated {safe_path}"
                        if safe_path is not None
                        else "Updating file"
                    )
                )
                metadata = {"activity": f"file_change.{phase}"}
                if safe_path is not None:
                    metadata["path"] = safe_path
            elif item_type == "agentMessage":
                message = f"Codex response {phase}"
                metadata = {"activity": f"agent_message.{phase}"}
        if message is None:
            return
        self._progress.emit(
            ProgressEvent(
                type=ProgressEventType.WORKER_ACTIVITY,
                timestamp=self._clock(),
                project_id=None,
                task_id=None,
                attempt=None,
                message=message,
                metadata=metadata,
            )
        )

    @staticmethod
    def _safe_command_message(item: dict[str, object]) -> str:
        actions = item.get("commandActions")
        if isinstance(actions, list) and actions:
            action_types = {
                action.get("type")
                for action in actions
                if isinstance(action, dict)
            }
            if action_types and action_types <= {"read", "listFiles", "search"}:
                return "Inspecting repository"
        return "Running command"

    def _safe_file_path(self, item: dict[str, object]) -> str | None:
        changes = item.get("changes")
        if not isinstance(changes, list) or not changes:
            return None
        first = changes[0]
        if not isinstance(first, dict):
            return None
        raw_path = first.get("path")
        if not isinstance(raw_path, str) or raw_path == "":
            return None
        workspace = self._config.workspace.resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            return candidate.resolve().relative_to(workspace).as_posix()
        except (OSError, ValueError):
            return None

    def close(self, *, reason: str = "closed") -> None:
        """Release the app-server deterministically and keep bounded evidence.

        Order matters: capture process status, stop owning the child, await its
        exit, then join the readers.  Callers may persist transport diagnostics
        before or after this call and will observe the same facts.
        """

        process = self._process
        self._closing = True
        if self._cleanup_reason is None:
            self._cleanup_reason = reason[:64]
        if process is None:
            self._state = TransportState.CLOSED
            return
        self._process = None
        self._state = TransportState.CLOSING
        self._capture_process_status(process)
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                self._stdin_state = ChannelState.FAILED
            else:
                self._stdin_state = ChannelState.CLOSED
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._capture_process_status(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        for thread in self._reader_threads:
            thread.join(timeout=1.0)
        self._reader_threads.clear()
        self._initialized = False
        if self._stdout_state is ChannelState.OPEN:
            self._stdout_state = ChannelState.CLOSED
        if self._stderr_state is ChannelState.OPEN:
            self._stderr_state = ChannelState.CLOSED
        self._state = TransportState.CLOSED

    def _capture_process_status(self, process: _Process) -> None:
        """Record exit code / signal exactly once, without guessing."""

        try:
            exit_code = process.poll()
        except (OSError, ValueError):
            return
        if exit_code is None:
            return
        self._app_server_exit_code = exit_code
        # A negative POSIX exit code is the terminating signal.
        if exit_code < 0:
            self._app_server_exit_signal = -exit_code

    def __enter__(self) -> CodexAppServerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._active_request_id = request_id
        self._record_event(
            method,
            TransportDirection.TO_WORKER,
            correlation_id=self._thread_id,
            payload_category="request",
        )
        self._write_message(request_message(request_id, method, params))
        deadline = self._monotonic() + self._config.inactivity_timeout_seconds
        while True:
            try:
                message = self._read_new_message(deadline)
            except CodexTurnTimeout as error:
                self._failure_kind = error.transport_failure_kind
                self._process_alive_at_failure = (
                    None if self._process is None else self._process.poll() is None
                )
                self.close(reason="request_timeout")
                raise
            except CodexParentInterrupted:
                self.close(reason="parent_interrupted")
                raise
            self._check_reader_health()
            kind = classify_message(message)
            if kind is MessageKind.RESPONSE:
                self._record_event(
                    "response",
                    TransportDirection.FROM_WORKER,
                    correlation_id=self._thread_id,
                    payload_category=(
                        "error" if "error" in message else "result"
                    ),
                )
                if "error" in message:
                    code = _safe_jsonrpc_error_code(
                        message["error"].get("code")
                        if isinstance(message["error"], dict)
                        else None
                    )
                    rejection = self._fail(
                        TransportFailureKind.REQUEST_REJECTED,
                        "app-server returned a structured request error",
                    )
                    assert isinstance(rejection, CodexRequestRejected)
                    rejection.request_id = request_id
                    if code is not None:
                        rejection.error_code = code
                    raise rejection
                return self._response_result(message, request_id)
            if kind is MessageKind.SERVER_REQUEST:
                self._record_event(
                    cast(str, message.get("method") or "server.request"),
                    TransportDirection.FROM_WORKER,
                    payload_category="server_request",
                )
                if self._raise_server_request(message):
                    continue
            self._pending_messages.append(message)

    def _response_result(
        self, message: dict[str, object], request_id: int
    ) -> dict[str, object]:
        response_id = message.get("id")
        if response_id != request_id:
            raise self._fail(
                TransportFailureKind.PROTOCOL_VIOLATION,
                f"response id {response_id!r} does not match request {request_id}",
            )
        result = message.get("result")
        if not isinstance(result, dict) or not all(
            isinstance(key, str) for key in result
        ):
            raise self._fail(
                TransportFailureKind.PROTOCOL_VIOLATION,
                "app-server response result must be an object",
            )
        return cast(dict[str, object], result)

    def _write_message(self, message: dict[str, object]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise self._fail(
                TransportFailureKind.STDIN_WRITE_FAILED,
                "Codex app-server stdin is unavailable",
            ) from error
        except ValueError as error:
            raise self._fail(
                TransportFailureKind.STDIN_WRITE_FAILED,
                "Codex app-server stdin is unavailable",
            ) from error

    def _next_message(self, deadline: float) -> dict[str, object]:
        if self._pending_messages:
            return self._pending_messages.popleft()
        return self._read_new_message(deadline)

    def _next_turn_message(
        self, hard_deadline: float, inactivity_deadline: float
    ) -> dict[str, object]:
        try:
            return self._next_message(min(hard_deadline, inactivity_deadline))
        except CodexTurnTimeout as error:
            if hard_deadline <= inactivity_deadline:
                raise CodexTurnHardTimeout(
                    "Codex turn reached its hard timeout",
                    inactivity_timeout_seconds=(
                        self._config.inactivity_timeout_seconds
                    ),
                    max_turn_seconds=self._config.max_turn_seconds,
                ) from error
            raise CodexTurnInactivityTimeout(
                "Codex turn reached its inactivity timeout",
                inactivity_timeout_seconds=(
                    self._config.inactivity_timeout_seconds
                ),
                max_turn_seconds=self._config.max_turn_seconds,
            ) from error

    def _read_new_message(self, deadline: float) -> dict[str, object]:
        timeout = deadline - self._monotonic()
        if timeout <= 0:
            raise CodexTurnTimeout("timed out waiting for Codex app-server")
        try:
            item = self._stdout_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise CodexTurnTimeout("timed out waiting for Codex app-server") from error
        if item is _EOF:
            self._stdout_state = ChannelState.EOF
            self._record_event(
                "stdout.eof", TransportDirection.FROM_WORKER
            )
            raise self._disconnect_error()
        if isinstance(item, BaseException):
            self._stdout_state = ChannelState.FAILED
            self._reader_failure_kind = _reader_failure_kind(item)
            self._record_event(
                "stdout.reader_failed", TransportDirection.FROM_WORKER
            )
            kind = (
                TransportFailureKind.PARENT_INTERRUPTED
                if isinstance(item, KeyboardInterrupt)
                else TransportFailureKind.STDOUT_READER_FAILED
            )
            raise self._fail(
                kind, "unable to read Codex app-server stdout"
            ) from item
        try:
            payload = json.loads(cast(str, item))
        except json.JSONDecodeError as error:
            self._record_event(
                "jsonrpc.decode_failed", TransportDirection.FROM_WORKER
            )
            raise self._fail(
                TransportFailureKind.JSONRPC_DECODE_FAILED,
                "Codex app-server emitted malformed JSON",
            ) from error
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise self._fail(
                TransportFailureKind.JSONRPC_DECODE_FAILED,
                "Codex app-server JSON must be an object",
            )
        return cast(dict[str, object], payload)

    def _disconnect_error(self) -> CodexWorkerError:
        """Separate a real process exit from an EOF on a live channel."""

        process = self._process
        if process is None:
            return self._fail(
                TransportFailureKind.APP_SERVER_DISCONNECTED,
                "Codex app-server stdout closed unexpectedly (exit None)",
            )
        exit_code = process.poll()
        if exit_code is None and self._eof_grace_seconds > 0:
            exit_code = self._await_exit_within_grace(process)
        self._capture_process_status(process)
        if exit_code is None:
            message = "Codex app-server stdout closed unexpectedly (exit None)"
            return self._fail(
                TransportFailureKind.APP_SERVER_DISCONNECTED, message
            )
        message = (
            f"Codex app-server stdout closed unexpectedly (exit {exit_code!r})"
        )
        kind = (
            TransportFailureKind.PROCESS_EXITED
            if exit_code != 0
            else TransportFailureKind.STDOUT_EOF
        )
        return self._fail(kind, message)

    def _await_exit_within_grace(self, process: _Process) -> int | None:
        """Bounded wait so EOF on a live pipe is classified, never guessed."""

        deadline = time.monotonic() + self._eof_grace_seconds
        while True:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            except (OSError, ValueError):
                return None
            exit_code = process.poll()
            if exit_code is not None:
                return exit_code
            if time.monotonic() >= deadline:
                return None
            time.sleep(_EOF_GRACE_POLL_SECONDS)

    def _is_trusted_turn_activity(
        self,
        method: str,
        params: dict[str, object],
        thread_id: str,
        turn_id: str,
    ) -> bool:
        if method == "turn/started":
            turn = params.get("turn")
            if not isinstance(params.get("threadId"), str) or not isinstance(
                turn, dict
            ):
                raise CodexProtocolError("turn/started has invalid identity")
            identity = turn.get("id")
            if not isinstance(identity, str):
                raise CodexProtocolError("turn/started has invalid identity")
            return params["threadId"] == thread_id and identity == turn_id
        if method in {"item/started", "item/completed"}:
            self._require_event_identity(params, method)
            if not isinstance(params.get("item"), dict):
                raise CodexProtocolError(f"{method} is missing item")
            return (
                params["threadId"] == thread_id
                and params["turnId"] == turn_id
            )
        if method == "item/agentMessage/delta":
            self._require_event_identity(params, method)
            delta = params.get("delta")
            if not isinstance(delta, str):
                raise CodexProtocolError("agentMessage delta must be text")
            # Only non-empty output from this turn is progress. Never retain
            # the streamed text; reasoning and retry notices stay excluded.
            return (
                bool(delta)
                and params["threadId"] == thread_id
                and params["turnId"] == turn_id
            )
        if method == "error":
            self._require_event_identity(params, method)
            # A provider retry is a protocol notification, not evidence that
            # the Worker made progress. Repeated retry notices must not keep
            # an otherwise idle turn alive until the hard deadline.
            return False
        return False

    def _read_stdout(self, stream: IO[str]) -> None:
        try:
            while True:
                line = stream.readline()
                if line == "":
                    self._stdout_queue.put(_EOF)
                    return
                self._stdout_queue.put(line)
        except BaseException as error:
            if self._closing:
                # A closed pipe during deterministic cleanup is not a failure.
                self._stdout_queue.put(_EOF)
                return
            self._stdout_queue.put(error)

    def _read_stderr(self, stream: IO[str]) -> None:
        """Always drain stderr so the app-server can never block on a full pipe."""

        try:
            for line in stream:
                self._stderr_lines.append(line.rstrip("\r\n"))
            self._stderr_state = ChannelState.EOF
        except (OSError, ValueError):
            self._stderr_state = ChannelState.CLOSED
        except BaseException:
            self._stderr_state = ChannelState.FAILED
            if self._reader_failure_kind is None:
                self._reader_failure_kind = "stderr_reader_failed"
            return

    def _raise_server_request(
        self,
        message: dict[str, object],
        *,
        expected_thread_id: str | None = None,
        expected_turn_id: str | None = None,
    ) -> bool:
        """Handle a live native approval or raise one typed stop boundary."""

        method = cast(str, message["method"])
        if method in APPROVAL_REQUEST_METHODS:
            request = parse_native_approval_request(message)
            if expected_thread_id is not None and request.thread_id != expected_thread_id:
                raise CodexProtocolError(
                    "native approval does not belong to the current thread"
                )
            if expected_turn_id is not None and request.turn_id != expected_turn_id:
                raise CodexProtocolError(
                    "native approval does not belong to the current turn"
                )
            # This server request can be answered only on its original live
            # JSON-RPC connection.  The one-shot CLI persists the typed gate
            # and closes the Worker; a later approve must therefore fail closed.
            raise CodexCapabilityApprovalRequired(request)
        if is_capability_approval_request(message):
            request = parse_capability_approval_request(message)
            if expected_thread_id is not None and request.thread_id != expected_thread_id:
                raise CodexProtocolError(
                    "capability approval does not belong to the current thread"
                )
            if expected_turn_id is not None and request.turn_id != expected_turn_id:
                raise CodexProtocolError(
                    "capability approval does not belong to the current turn"
                )
            if self._capability_approval_handler is None:
                raise CodexCapabilityApprovalRequired(request)
            try:
                decision = self._capability_approval_handler(request)
            except Exception:
                raise CodexProtocolError(
                    "capability approval handler failed closed"
                ) from None
            if not isinstance(decision, CapabilityApprovalDecision):
                raise CodexProtocolError(
                    "capability approval handler returned an invalid decision"
                )
            if (
                decision.scope is not None
                and decision.scope not in request.available_scopes
            ):
                raise CodexProtocolError(
                    "capability approval selected a scope that was not offered"
                )
            self._write_message(
                server_response_message(request.protocol_request_id, decision)
            )
            return True
        if method in USER_INPUT_REQUEST_METHODS:
            raise CodexUserInputRequired(parse_worker_input_request(message))
        raise CodexProtocolError(f"unsupported app-server request method {method!r}")

    @staticmethod
    def _params(message: dict[str, object], method: str) -> dict[str, object]:
        params = message.get("params")
        if not isinstance(params, dict) or not all(
            isinstance(key, str) for key in params
        ):
            raise CodexProtocolError(f"{method} params must be an object")
        return cast(dict[str, object], params)

    @staticmethod
    def _require_event_identity(params: dict[str, object], method: str) -> None:
        if not isinstance(params.get("threadId"), str):
            raise CodexProtocolError(f"{method} is missing threadId")
        if not isinstance(params.get("turnId"), str):
            raise CodexProtocolError(f"{method} is missing turnId")

    def _require_process(self) -> _Process:
        if self._process is None:
            raise CodexAppServerStartError("Codex app-server is not running")
        return self._process

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise CodexProtocolError("Codex app-server is not initialized")


__all__ = ["CodexAppServerClient"]
