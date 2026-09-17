"""Public contracts and fail-closed errors for Codex Worker execution."""

from dataclasses import dataclass
from pathlib import Path
from enum import StrEnum
import math
import re

from code_mule.domain.enums import CapabilityApprovalScope
from code_mule.domain.models import Task

from code_mule.transport import (
    TransportFailureKind,
    WorkerFailureClass,
    failure_class_for,
)


def _require_non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


class CodexWorkerError(RuntimeError):
    """Base class for Codex Worker boundary failures.

    Every concrete boundary failure carries the exact typed transport reason
    it represents, so no caller has to infer one (or fall back to Unknown).
    """

    transport_failure_kind: TransportFailureKind | None = None

    @property
    def failure_class(self) -> WorkerFailureClass | None:
        return failure_class_for(self.transport_failure_kind)


class CodexAppServerStartError(CodexWorkerError):
    """Raised when the local Codex app-server cannot be started."""

    transport_failure_kind = TransportFailureKind.APP_SERVER_START_FAILED


class CodexProtocolError(CodexWorkerError):
    """Raised when app-server violates the expected structured protocol."""

    transport_failure_kind = TransportFailureKind.PROTOCOL_VIOLATION


class CodexAppServerDisconnected(CodexProtocolError):
    """Raised when the app-server stdio channel ends without a terminal turn."""


class CodexStdoutReaderFailed(CodexProtocolError):
    """Raised when the app-server stdout reader itself fails."""

    transport_failure_kind = TransportFailureKind.STDOUT_READER_FAILED


class CodexStdinWriteFailed(CodexProtocolError):
    """Raised when a structured request cannot be written to the app-server."""

    transport_failure_kind = TransportFailureKind.STDIN_WRITE_FAILED


class CodexJsonRpcDecodeError(CodexProtocolError):
    """Raised when one app-server line is not a valid JSON-RPC object."""

    transport_failure_kind = TransportFailureKind.JSONRPC_DECODE_FAILED


class CodexRequestRejected(CodexWorkerError):
    """Raised when the app-server rejects a structured JSON-RPC request."""

    transport_failure_kind = TransportFailureKind.REQUEST_REJECTED

    def __init__(self, message: str, *, request_id: int | None = None) -> None:
        self.request_id = request_id
        super().__init__(message)


class CodexParentInterrupted(CodexWorkerError):
    """Raised when the parent CLI interrupt boundary stops a live Worker."""

    transport_failure_kind = TransportFailureKind.PARENT_INTERRUPTED


class CodexTurnFailureKind(StrEnum):
    ERROR_NOTIFICATION = "error_notification"
    TURN_FAILED = "turn_failed"
    TURN_INTERRUPTED = "turn_interrupted"


# Only exact structured codes are retained; unknown codes and prose are omitted.
SAFE_TURN_ERROR_CODES = frozenset({
    "rate_limit_exceeded", "context_window_exceeded", "internal_error",
    "server_error", "model_not_found", "insufficient_quota",
})


@dataclass(frozen=True)
class CodexTurnFailureDetails:
    kind: CodexTurnFailureKind
    thread_id: str
    turn_id: str
    turn_status: str | None
    will_retry: bool | None
    error_code: str | None
    activity_count: int
    last_activity_age_seconds: float | None
    turn_elapsed_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CodexTurnFailureKind):
            raise ValueError("failure kind must be typed")
        for value in (self.thread_id, self.turn_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                raise ValueError("failure identity must be bounded")
        expected = {
            CodexTurnFailureKind.ERROR_NOTIFICATION: None,
            CodexTurnFailureKind.TURN_FAILED: "failed",
            CodexTurnFailureKind.TURN_INTERRUPTED: "interrupted",
        }[self.kind]
        if self.turn_status != expected:
            raise ValueError("failure kind and terminal status disagree")
        if self.will_retry is not None and type(self.will_retry) is not bool:
            raise ValueError("will_retry must be boolean or None")
        if self.will_retry is True or (expected is not None and self.will_retry is not None):
            raise ValueError("terminal failure cannot request retry")
        if self.error_code is not None and self.error_code not in SAFE_TURN_ERROR_CODES:
            raise ValueError("error code is not allowlisted")
        if type(self.activity_count) is not int or not 0 <= self.activity_count <= 1_000_000_000:
            raise ValueError("activity count must be bounded")
        if self.turn_elapsed_seconds is None:
            raise ValueError("turn elapsed time is required")
        for value in (self.turn_elapsed_seconds, self.last_activity_age_seconds):
            if value is not None and (
                type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1_000_000_000
            ):
                raise ValueError("failure elapsed time must be finite and bounded")
        if (self.activity_count == 0) != (self.last_activity_age_seconds is None):
            raise ValueError("last activity requires observed activity")
        if self.last_activity_age_seconds is not None and self.last_activity_age_seconds > self.turn_elapsed_seconds:
            raise ValueError("activity cannot precede turn observation")


class CodexTurnFailed(CodexWorkerError):
    """Raised when app-server explicitly reports a failed or interrupted turn."""

    def __init__(self, message: str = "Codex turn failed", *, details: CodexTurnFailureDetails | None = None) -> None:
        self.details = details
        self.transport_failure_kind = (
            None
            if details is None
            else {
                CodexTurnFailureKind.ERROR_NOTIFICATION: TransportFailureKind.ERROR_NOTIFICATION,
                CodexTurnFailureKind.TURN_FAILED: TransportFailureKind.TURN_FAILED,
                CodexTurnFailureKind.TURN_INTERRUPTED: TransportFailureKind.TURN_INTERRUPTED,
            }[details.kind]
        )
        # Preserve source compatibility without retaining arbitrary provider prose.
        super().__init__("Codex turn failed" if details is None else f"Codex turn failed ({details.kind.value})")


class CodexTurnTimeout(CodexWorkerError):
    """Raised when a Codex response or turn exceeds its configured deadline."""

    timeout_kind = "operation"
    transport_failure_kind = TransportFailureKind.INACTIVITY_TIMEOUT

    def __init__(
        self,
        message: str,
        *,
        inactivity_timeout_seconds: float | None = None,
        max_turn_seconds: float | None = None,
    ) -> None:
        self.inactivity_timeout_seconds = inactivity_timeout_seconds
        self.max_turn_seconds = max_turn_seconds
        super().__init__(message)


class CodexTurnInactivityTimeout(CodexTurnTimeout):
    """Raised when a turn produces no trusted activity before its idle limit."""

    timeout_kind = "inactivity"
    transport_failure_kind = TransportFailureKind.INACTIVITY_TIMEOUT


class CodexTurnHardTimeout(CodexTurnTimeout):
    """Raised when a turn reaches its non-refreshable maximum duration."""

    timeout_kind = "hard"
    transport_failure_kind = TransportFailureKind.HARD_TIMEOUT


@dataclass(frozen=True)
class WorkerTurnTerminal:
    """Trusted terminal evidence captured before any report parsing.

    Code Mule persists this the moment a terminal turn event is observed so a
    later parser or persistence failure can never look like a missing
    terminal result.
    """

    thread_id: str
    turn_id: str
    terminal_event_type: str
    turn_status: str
    activity_count: int
    event_count: int
    final_message_present: bool

    def __post_init__(self) -> None:
        for value in (self.thread_id, self.turn_id):
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", value
            ):
                raise ValueError("terminal identity must be bounded")
        if self.terminal_event_type != "turn/completed":
            # The app-server protocol defines exactly one terminal turn
            # notification; the outcome lives in ``turn.status``.
            raise ValueError("terminal event type must be turn/completed")
        if self.turn_status not in {"completed", "failed", "interrupted"}:
            raise ValueError("terminal turn status must be known")
        if type(self.activity_count) is not int or not (
            0 <= self.activity_count <= 1_000_000_000
        ):
            raise ValueError("terminal activity count must be bounded")
        if type(self.event_count) is not int or not (
            0 <= self.event_count <= 1_000_000_000
        ):
            raise ValueError("terminal event count must be bounded")
        if type(self.final_message_present) is not bool:
            raise ValueError("final message flag must be boolean")

    @property
    def report_parse_required(self) -> bool:
        return self.turn_status == "completed"


def worker_failure_metadata(error: CodexWorkerError) -> dict[str, str]:
    """Return bounded diagnostic metadata without model or environment content."""

    metadata = {"error_type": type(error).__name__}
    kind = error.transport_failure_kind
    if kind is not None:
        metadata["transport_failure_kind"] = kind.value
    failure_class = error.failure_class
    if failure_class is not None:
        metadata["failure_class"] = failure_class.value
    if isinstance(error, CodexRequestRejected) and error.request_id is not None:
        metadata["request_id"] = str(error.request_id)
    if isinstance(error, CodexTurnFailed) and error.details is not None:
        details = error.details
        metadata.update({
            "failure_kind": details.kind.value,
            "thread_id": details.thread_id,
            "turn_id": details.turn_id,
            "activity_count": str(details.activity_count),
            "turn_elapsed_seconds": format(details.turn_elapsed_seconds, ".6f"),
        })
        if details.turn_status is not None:
            metadata["turn_status"] = details.turn_status
        if details.will_retry is not None:
            metadata["will_retry"] = str(details.will_retry).lower()
        if details.error_code is not None:
            metadata["error_code"] = details.error_code
        if details.last_activity_age_seconds is not None:
            metadata["last_activity_age_seconds"] = format(details.last_activity_age_seconds, ".6f")
    if isinstance(error, CodexTurnTimeout):
        metadata["timeout_kind"] = error.timeout_kind
        if error.inactivity_timeout_seconds is not None:
            metadata["inactivity_timeout_seconds"] = format(
                error.inactivity_timeout_seconds, "g"
            )
        if error.max_turn_seconds is not None:
            metadata["max_turn_seconds"] = format(error.max_turn_seconds, "g")
    terminal = getattr(error, "terminal", None)
    if not isinstance(terminal, WorkerTurnTerminal):
        terminal = None
    if terminal is not None:
        metadata.update(
            {
                "terminal_event_received": "true",
                "terminal_event_type": terminal.terminal_event_type,
                "thread_id": terminal.thread_id,
                "turn_id": terminal.turn_id,
                "activity_count": str(terminal.activity_count),
                "report_parse_failed": "true",
            }
        )
    stage = getattr(error, "stage", None)
    code = getattr(error, "code", None)
    if stage is not None or code is not None:
        metadata["report_parse_failed"] = "true"
        if stage is not None:
            metadata["report_stage"] = _bounded_diagnostic(getattr(stage, "value", stage))
        if code is not None:
            metadata["report_code"] = _bounded_diagnostic(getattr(code, "value", code))
        field_path = getattr(error, "field_path", None)
        if isinstance(field_path, str) and 0 < len(field_path) <= 160:
            metadata["report_field_path"] = field_path
        for name, attribute in (
            ("candidate_found", "candidate_found"),
            ("json_decoded", "json_decoded"),
            ("semantic_validation_started", "semantic_validation_started"),
            ("final_message_present", "final_message_present"),
        ):
            value = getattr(error, attribute, None)
            if type(value) is bool:
                metadata[name] = str(value).lower()
    return metadata


def _bounded_diagnostic(value: object) -> str:
    text = str(value)
    return text if len(text) <= 64 else text[:64]


def turn_failure_details_from_metadata(metadata: dict[str, str]) -> CodexTurnFailureDetails | None:
    """Validate the safe event projection before verbose display; never echo extras."""
    if metadata.get("error_type") != "CodexTurnFailed":
        return None
    try:
        retry = metadata.get("will_retry")
        if retry not in (None, "false"):
            return None
        return CodexTurnFailureDetails(
            kind=CodexTurnFailureKind(metadata["failure_kind"]),
            thread_id=metadata["thread_id"], turn_id=metadata["turn_id"],
            turn_status=metadata.get("turn_status"),
            will_retry=False if retry == "false" else None,
            error_code=metadata.get("error_code"),
            activity_count=int(metadata["activity_count"]),
            last_activity_age_seconds=(
                float(metadata["last_activity_age_seconds"])
                if "last_activity_age_seconds" in metadata else None
            ),
            turn_elapsed_seconds=float(metadata["turn_elapsed_seconds"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class CodexApprovalRequired(CodexWorkerError):
    """Raised when app-server requests an approval Code Mule cannot grant."""


class CapabilityApprovalAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


@dataclass(frozen=True)
class CapabilityApprovalDecision:
    """One protocol-native decision for a still-live server request."""

    action: CapabilityApprovalAction
    scope: CapabilityApprovalScope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, CapabilityApprovalAction):
            raise ValueError("capability approval action must be typed")
        if self.scope is not None and not isinstance(
            self.scope, CapabilityApprovalScope
        ):
            raise ValueError("capability approval scope must be typed")
        if self.action is not CapabilityApprovalAction.ACCEPT and self.scope is not None:
            raise ValueError("only an accepted approval can select a scope")


@dataclass(frozen=True)
class WorkerCapabilityApprovalRequest:
    """Bounded projection of a native MCP capability approval request."""

    method: str
    protocol_request_id: int | str
    thread_id: str
    turn_id: str
    server_name: str
    request: str
    capability: str
    application: str | None
    capability_id: str | None
    tool_name: str | None
    available_scopes: tuple[CapabilityApprovalScope, ...]

    def __post_init__(self) -> None:
        required = {
            "method": (self.method, 128),
            "thread_id": (self.thread_id, 128),
            "turn_id": (self.turn_id, 128),
            "server_name": (self.server_name, 200),
            "request": (self.request, 2_000),
            "capability": (self.capability, 200),
        }
        optional = {
            "application": (self.application, 200),
            "capability_id": (self.capability_id, 200),
            "tool_name": (self.tool_name, 200),
        }
        if isinstance(self.protocol_request_id, bool) or not isinstance(
            self.protocol_request_id, (int, str)
        ):
            raise ValueError("protocol_request_id must be a string or integer")
        if isinstance(self.protocol_request_id, int) and self.protocol_request_id < 0:
            raise ValueError("protocol_request_id must not be negative")
        if len(str(self.protocol_request_id)) > 128 or str(self.protocol_request_id) == "":
            raise ValueError("protocol_request_id exceeds safe bounds")
        for name, (value, limit) in required.items():
            _require_non_empty(value, name)
            if len(value) > limit:
                raise ValueError(f"{name} exceeds safe bounds")
        for name, (value, limit) in optional.items():
            if value is not None:
                _require_non_empty(value, name)
                if len(value) > limit:
                    raise ValueError(f"{name} exceeds safe bounds")
        if len(self.available_scopes) > 3 or len(set(self.available_scopes)) != len(
            self.available_scopes
        ):
            raise ValueError("available_scopes must be unique and bounded")
        if any(
            not isinstance(scope, CapabilityApprovalScope)
            for scope in self.available_scopes
        ):
            raise ValueError("available_scopes must be typed")

    @property
    def request_id(self) -> str:
        return str(self.protocol_request_id)


class CodexCapabilityApprovalRequired(CodexApprovalRequired):
    """Raised when a native capability request cannot remain live for approval."""

    def __init__(self, request: WorkerCapabilityApprovalRequest) -> None:
        self.request = request
        super().__init__(
            f"Codex app-server requested native capability approval via {request.method}"
        )


@dataclass(frozen=True)
class WorkerInputRequest:
    """Bounded safe projection of one app-server human-input request."""

    method: str
    request_id: str | None
    question: str
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.method, "method")
        _require_non_empty(self.question, "question")
        if len(self.method) > 128:
            raise ValueError("method must not exceed 128 characters")
        if self.request_id is not None:
            _require_non_empty(self.request_id, "request_id")
            if len(self.request_id) > 128:
                raise ValueError("request_id must not exceed 128 characters")
        if len(self.question) > 2_000:
            raise ValueError("question must not exceed 2000 characters")
        if len(self.choices) > 20:
            raise ValueError("choices must not contain more than 20 values")
        if any(choice == "" or len(choice) > 500 for choice in self.choices):
            raise ValueError("choices must contain bounded non-empty values")


class CodexUserInputRequired(CodexWorkerError):
    """Raised when app-server requests interactive user input."""

    def __init__(self, request: WorkerInputRequest) -> None:
        self.request = request
        super().__init__(
            f"Codex app-server requested user input via {request.method}"
        )


@dataclass(frozen=True)
class CodexWorkerConfig:
    command: tuple[str, ...]
    workspace: Path
    approval_policy: str
    sandbox: str
    inactivity_timeout_seconds: float
    max_turn_seconds: float

    def __post_init__(self) -> None:
        if not self.command or any(part == "" for part in self.command):
            raise ValueError("command must contain only non-empty arguments")
        if not self.workspace.is_absolute():
            raise ValueError("workspace must be absolute")
        _require_non_empty(self.approval_policy, "approval_policy")
        _require_non_empty(self.sandbox, "sandbox")
        if self.inactivity_timeout_seconds <= 0:
            raise ValueError("inactivity_timeout_seconds must be positive")
        if self.max_turn_seconds <= 0:
            raise ValueError("max_turn_seconds must be positive")
        if self.max_turn_seconds < self.inactivity_timeout_seconds:
            raise ValueError(
                "max_turn_seconds must be at least inactivity_timeout_seconds"
            )


@dataclass(frozen=True)
class WorkerTaskRequest:
    task: Task
    prompt: str
    title: str

    def __post_init__(self) -> None:
        _require_non_empty(self.prompt, "prompt")
        _require_non_empty(self.title, "title")


@dataclass(frozen=True)
class WorkerTurnResult:
    thread_id: str
    turn_id: str
    final_message: str | None
    completed: bool
    event_count: int
    issues: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.thread_id, "thread_id")
        _require_non_empty(self.turn_id, "turn_id")
        if self.event_count < 0:
            raise ValueError("event_count must be non-negative")


__all__ = [
    "CapabilityApprovalAction",
    "CapabilityApprovalDecision",
    "CodexAppServerDisconnected",
    "CodexAppServerStartError",
    "CodexApprovalRequired",
    "CodexCapabilityApprovalRequired",
    "CodexJsonRpcDecodeError",
    "CodexParentInterrupted",
    "CodexProtocolError",
    "CodexRequestRejected",
    "CodexStdinWriteFailed",
    "CodexStdoutReaderFailed",
    "CodexTurnFailed",
    "CodexTurnFailureKind",
    "CodexTurnFailureDetails",
    "CodexTurnHardTimeout",
    "CodexTurnInactivityTimeout",
    "CodexTurnTimeout",
    "CodexUserInputRequired",
    "CodexWorkerConfig",
    "CodexWorkerError",
    "WorkerInputRequest",
    "WorkerCapabilityApprovalRequest",
    "WorkerTaskRequest",
    "WorkerTurnTerminal",
    "WorkerTurnResult",
    "worker_failure_metadata",
]
