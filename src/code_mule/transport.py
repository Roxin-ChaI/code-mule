"""Typed, bounded supervision evidence for the Codex Worker transport.

This module owns two things and nothing else:

* the exact vocabulary used to name a Worker transport outcome, so no new
  code path can fall back to an untyped ``Unknown`` stop cause;
* the bounded, metadata-only diagnostics projection persisted with a Worker
  attempt so a real failure stays forensically readable.

It never carries raw stdout, model output, prompts, reasoning, stderr text,
or credentials.  Only typed identifiers, counters, timings, and an allowlisted
protocol event ring are representable.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import math
import re


MAX_EVENT_RING = 20
MAX_EVENT_TYPE_LENGTH = 120
MAX_CORRELATION_LENGTH = 128
MAX_CATEGORY_LENGTH = 64
MAX_CLEANUP_REASON_LENGTH = 64

_EVENT_TYPE = re.compile(r"[a-z0-9][a-z0-9_./-]{0,119}")
_CORRELATION = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CATEGORY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


class TransportState(StrEnum):
    """Lifecycle position of one Worker transport attempt."""

    PROCESS_STARTING = "process_starting"
    PROCESS_RUNNING = "process_running"
    CHANNEL_READY = "channel_ready"
    THREAD_CREATED = "thread_created"
    TURN_STARTED = "turn_started"
    TURN_ACTIVE = "turn_active"
    TURN_TERMINAL = "turn_terminal"
    REPORT_PARSED = "report_parsed"
    REPORT_PERSISTED = "report_persisted"
    CLOSING = "closing"
    CLOSED = "closed"


class TransportFailureKind(StrEnum):
    """Exact reason a Worker transport stopped producing trusted evidence."""

    APP_SERVER_START_FAILED = "app_server_start_failed"
    PROCESS_EXITED = "process_exited"
    STDOUT_EOF = "stdout_eof"
    STDOUT_READER_FAILED = "stdout_reader_failed"
    STDERR_READER_FAILED = "stderr_reader_failed"
    STDIN_WRITE_FAILED = "stdin_write_failed"
    JSONRPC_DECODE_FAILED = "jsonrpc_decode_failed"
    PROTOCOL_VIOLATION = "protocol_violation"
    REQUEST_REJECTED = "request_rejected"
    TURN_FAILED = "turn_failed"
    TURN_INTERRUPTED = "turn_interrupted"
    ERROR_NOTIFICATION = "error_notification"
    INACTIVITY_TIMEOUT = "inactivity_timeout"
    HARD_TIMEOUT = "hard_timeout"
    PARENT_INTERRUPTED = "parent_interrupted"
    TRANSPORT_CANCELLED = "transport_cancelled"
    APP_SERVER_DISCONNECTED = "app_server_disconnected"
    REPORT_PARSE_FAILED = "report_parse_failed"
    REPORT_PERSIST_FAILED = "report_persist_failed"
    UNCLASSIFIED_PROTOCOL_FAILURE = "unclassified_protocol_failure"


class ChannelState(StrEnum):
    """Observable state of one app-server stdio channel."""

    UNOPENED = "unopened"
    OPEN = "open"
    EOF = "eof"
    FAILED = "failed"
    CLOSED = "closed"


class WorkerFailureClass(StrEnum):
    """Who is ultimately responsible for a stopped Worker attempt."""

    CODEX_TURN_FAILURE = "codex_turn_failure"
    CODEX_PROCESS_FAILURE = "codex_process_failure"
    TRANSPORT_FAILURE = "transport_failure"
    CODE_MULE_RUNTIME_FAILURE = "code_mule_runtime_failure"
    TIMEOUT = "timeout"
    USER_INTERRUPT = "user_interrupt"


class TransportDirection(StrEnum):
    TO_WORKER = "to_worker"
    FROM_WORKER = "from_worker"


# Which failure class each transport failure kind belongs to.  Kept explicit
# so a new kind cannot silently inherit a wrong owner.
FAILURE_CLASS_BY_KIND: dict[TransportFailureKind, WorkerFailureClass] = {
    TransportFailureKind.APP_SERVER_START_FAILED: WorkerFailureClass.CODEX_PROCESS_FAILURE,
    TransportFailureKind.PROCESS_EXITED: WorkerFailureClass.CODEX_PROCESS_FAILURE,
    TransportFailureKind.STDOUT_EOF: WorkerFailureClass.TRANSPORT_FAILURE,
    TransportFailureKind.STDOUT_READER_FAILED: WorkerFailureClass.TRANSPORT_FAILURE,
    TransportFailureKind.STDERR_READER_FAILED: WorkerFailureClass.TRANSPORT_FAILURE,
    TransportFailureKind.STDIN_WRITE_FAILED: WorkerFailureClass.TRANSPORT_FAILURE,
    TransportFailureKind.JSONRPC_DECODE_FAILED: WorkerFailureClass.TRANSPORT_FAILURE,
    TransportFailureKind.PROTOCOL_VIOLATION: WorkerFailureClass.TRANSPORT_FAILURE,
    TransportFailureKind.REQUEST_REJECTED: WorkerFailureClass.CODEX_TURN_FAILURE,
    TransportFailureKind.TURN_FAILED: WorkerFailureClass.CODEX_TURN_FAILURE,
    TransportFailureKind.TURN_INTERRUPTED: WorkerFailureClass.CODEX_TURN_FAILURE,
    TransportFailureKind.ERROR_NOTIFICATION: WorkerFailureClass.CODEX_TURN_FAILURE,
    TransportFailureKind.INACTIVITY_TIMEOUT: WorkerFailureClass.TIMEOUT,
    TransportFailureKind.HARD_TIMEOUT: WorkerFailureClass.TIMEOUT,
    TransportFailureKind.PARENT_INTERRUPTED: WorkerFailureClass.USER_INTERRUPT,
    TransportFailureKind.TRANSPORT_CANCELLED: WorkerFailureClass.CODE_MULE_RUNTIME_FAILURE,
    TransportFailureKind.APP_SERVER_DISCONNECTED: WorkerFailureClass.TRANSPORT_FAILURE,
    # The report contract is Code Mule's own boundary: a rejected Worker report
    # is a runtime/report-contract failure, never a transport failure.
    TransportFailureKind.REPORT_PARSE_FAILED: WorkerFailureClass.CODE_MULE_RUNTIME_FAILURE,
    TransportFailureKind.REPORT_PERSIST_FAILED: WorkerFailureClass.CODE_MULE_RUNTIME_FAILURE,
    TransportFailureKind.UNCLASSIFIED_PROTOCOL_FAILURE: WorkerFailureClass.TRANSPORT_FAILURE,
}


def failure_class_for(kind: TransportFailureKind | None) -> WorkerFailureClass | None:
    if kind is None:
        return None
    return FAILURE_CLASS_BY_KIND.get(kind)


def _bounded_text(
    value: str | None, pattern: re.Pattern[str], limit: int
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or not pattern.fullmatch(value):
        raise ValueError("transport diagnostic text is not bounded")
    return value


@dataclass(frozen=True)
class TransportEventRecord:
    """One metadata-only protocol lifecycle observation."""

    at: datetime
    event_type: str
    direction: TransportDirection
    correlation_id: str | None = None
    payload_category: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.at, datetime):
            raise ValueError("event timestamp must be a datetime")
        _bounded_text(self.event_type, _EVENT_TYPE, MAX_EVENT_TYPE_LENGTH)
        if not isinstance(self.direction, TransportDirection):
            raise ValueError("event direction must be typed")
        _bounded_text(self.correlation_id, _CORRELATION, MAX_CORRELATION_LENGTH)
        _bounded_text(self.payload_category, _CATEGORY, MAX_CATEGORY_LENGTH)


@dataclass(frozen=True)
class TransportDiagnostics:
    """Bounded, secret-free facts about one Worker transport attempt."""

    stdout_state: ChannelState
    stderr_state: ChannelState
    stdin_state: ChannelState
    terminal_event_received: bool
    activity_count: int
    app_server_pid: int | None = None
    app_server_started_at: datetime | None = None
    app_server_command: str | None = None
    app_server_exit_code: int | None = None
    app_server_exit_signal: int | None = None
    transport_failure_kind: TransportFailureKind | None = None
    failure_class: WorkerFailureClass | None = None
    last_protocol_event_type: str | None = None
    last_protocol_event_at: datetime | None = None
    terminal_event_type: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    request_id: int | None = None
    last_activity_at: datetime | None = None
    reader_failure_kind: str | None = None
    process_alive_at_failure: bool | None = None
    cleanup_reason: str | None = None
    events: tuple[TransportEventRecord, ...] = ()
    legacy_transport_evidence_incomplete: bool = False

    def __post_init__(self) -> None:
        for name in ("stdout_state", "stderr_state", "stdin_state"):
            if not isinstance(getattr(self, name), ChannelState):
                raise ValueError(f"{name} must be typed")
        if type(self.terminal_event_received) is not bool:
            raise ValueError("terminal_event_received must be boolean")
        if type(self.legacy_transport_evidence_incomplete) is not bool:
            raise ValueError("legacy_transport_evidence_incomplete must be boolean")
        if (
            type(self.activity_count) is not int
            or not 0 <= self.activity_count <= 1_000_000_000
        ):
            raise ValueError("activity_count must be bounded")
        for name in ("app_server_pid", "request_id"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("app_server_exit_code", "app_server_exit_signal"):
            value = getattr(self, name)
            if value is not None and type(value) is not int:
                raise ValueError(f"{name} must be an integer")
        if self.app_server_exit_code is not None and not (
            -1_000_000 <= self.app_server_exit_code <= 1_000_000
        ):
            raise ValueError("app_server_exit_code must be bounded")
        if self.app_server_exit_signal is not None and not (
            0 <= self.app_server_exit_signal <= 1_000
        ):
            raise ValueError("app_server_exit_signal must be bounded")
        if self.transport_failure_kind is not None and not isinstance(
            self.transport_failure_kind, TransportFailureKind
        ):
            raise ValueError("transport_failure_kind must be typed")
        if self.failure_class is not None:
            if not isinstance(self.failure_class, WorkerFailureClass):
                raise ValueError("failure_class must be typed")
            expected = failure_class_for(self.transport_failure_kind)
            if self.transport_failure_kind is not None and self.failure_class is not expected:
                raise ValueError("failure class disagrees with its transport kind")
        if self.transport_failure_kind is not None and self.failure_class is None:
            raise ValueError("transport failure kind requires a failure class")
        if self.terminal_event_received != (self.terminal_event_type is not None):
            raise ValueError("terminal event type must match its received flag")
        if self.activity_count == 0 and self.last_activity_at is not None:
            raise ValueError("last activity requires observed activity")
        if self.activity_count > 0 and self.last_activity_at is None:
            raise ValueError("observed activity requires a last activity time")
        if not isinstance(self.events, tuple) or len(self.events) > MAX_EVENT_RING:
            raise ValueError("transport event ring must be bounded")
        for event in self.events:
            if not isinstance(event, TransportEventRecord):
                raise ValueError("transport event ring must be typed")
        _bounded_text(
            self.last_protocol_event_type, _EVENT_TYPE, MAX_EVENT_TYPE_LENGTH
        )
        _bounded_text(self.terminal_event_type, _EVENT_TYPE, MAX_EVENT_TYPE_LENGTH)
        _bounded_text(self.thread_id, _CORRELATION, MAX_CORRELATION_LENGTH)
        _bounded_text(self.turn_id, _CORRELATION, MAX_CORRELATION_LENGTH)
        _bounded_text(self.reader_failure_kind, _CATEGORY, MAX_CATEGORY_LENGTH)
        if self.app_server_command is not None:
            if (
                not isinstance(self.app_server_command, str)
                or not 1 <= len(self.app_server_command) <= 240
                or "\n" in self.app_server_command
            ):
                raise ValueError("app_server_command must be bounded")
        _bounded_text(
            self.cleanup_reason, _CATEGORY, MAX_CLEANUP_REASON_LENGTH
        )
        if self.process_alive_at_failure is not None and type(
            self.process_alive_at_failure
        ) is not bool:
            raise ValueError("process_alive_at_failure must be boolean or None")
        for name in (
            "app_server_started_at",
            "last_protocol_event_at",
            "last_activity_at",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, datetime):
                raise ValueError(f"{name} must be a datetime")

    @property
    def is_legacy_incomplete(self) -> bool:
        return self.legacy_transport_evidence_incomplete


def legacy_transport_diagnostics() -> TransportDiagnostics:
    """Explicit marker for upgraded state that has no captured transport facts."""

    return TransportDiagnostics(
        stdout_state=ChannelState.UNOPENED,
        stderr_state=ChannelState.UNOPENED,
        stdin_state=ChannelState.UNOPENED,
        terminal_event_received=False,
        activity_count=0,
        legacy_transport_evidence_incomplete=True,
    )


__all__ = [
    "ChannelState",
    "FAILURE_CLASS_BY_KIND",
    "MAX_EVENT_RING",
    "TransportDiagnostics",
    "TransportDirection",
    "TransportEventRecord",
    "TransportFailureKind",
    "TransportState",
    "WorkerFailureClass",
    "failure_class_for",
    "legacy_transport_diagnostics",
]
