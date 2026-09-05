"""Public contracts and fail-closed errors for Codex Worker execution."""

from dataclasses import dataclass
from pathlib import Path
from enum import StrEnum
import math
import re

from code_mule.domain.models import Task


def _require_non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


class CodexWorkerError(RuntimeError):
    """Base class for Codex Worker boundary failures."""


class CodexAppServerStartError(CodexWorkerError):
    """Raised when the local Codex app-server cannot be started."""


class CodexProtocolError(CodexWorkerError):
    """Raised when app-server violates the expected structured protocol."""


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
        # Preserve source compatibility without retaining arbitrary provider prose.
        super().__init__("Codex turn failed" if details is None else f"Codex turn failed ({details.kind.value})")


class CodexTurnTimeout(CodexWorkerError):
    """Raised when a Codex response or turn exceeds its configured deadline."""

    timeout_kind = "operation"

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


class CodexTurnHardTimeout(CodexTurnTimeout):
    """Raised when a turn reaches its non-refreshable maximum duration."""

    timeout_kind = "hard"


def worker_failure_metadata(error: CodexWorkerError) -> dict[str, str]:
    """Return bounded diagnostic metadata without model or environment content."""

    metadata = {"error_type": type(error).__name__}
    if isinstance(error, CodexTurnTimeout):
        metadata["timeout_kind"] = error.timeout_kind
        if error.inactivity_timeout_seconds is not None:
            metadata["inactivity_timeout_seconds"] = format(
                error.inactivity_timeout_seconds, "g"
            )
        if error.max_turn_seconds is not None:
            metadata["max_turn_seconds"] = format(error.max_turn_seconds, "g")
    return metadata


class CodexApprovalRequired(CodexWorkerError):
    """Raised when app-server requests an approval Code Mule cannot grant."""


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
    "CodexAppServerStartError",
    "CodexApprovalRequired",
    "CodexProtocolError",
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
    "WorkerTaskRequest",
    "WorkerTurnResult",
    "worker_failure_metadata",
]
