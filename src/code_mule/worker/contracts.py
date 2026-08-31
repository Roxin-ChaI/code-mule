"""Public contracts and fail-closed errors for Codex Worker execution."""

from dataclasses import dataclass
from pathlib import Path

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


class CodexTurnFailed(CodexWorkerError):
    """Raised when app-server explicitly reports a failed or interrupted turn."""


class CodexTurnTimeout(CodexWorkerError):
    """Raised when a Codex response or turn exceeds its configured deadline."""


class CodexApprovalRequired(CodexWorkerError):
    """Raised when app-server requests an approval Code Mule cannot grant."""


class CodexUserInputRequired(CodexWorkerError):
    """Raised when app-server requests interactive user input."""


@dataclass(frozen=True)
class CodexWorkerConfig:
    command: tuple[str, ...]
    workspace: Path
    approval_policy: str
    sandbox: str
    read_timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.command or any(part == "" for part in self.command):
            raise ValueError("command must contain only non-empty arguments")
        if not self.workspace.is_absolute():
            raise ValueError("workspace must be absolute")
        _require_non_empty(self.approval_policy, "approval_policy")
        _require_non_empty(self.sandbox, "sandbox")
        if self.read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")


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
    "CodexTurnTimeout",
    "CodexUserInputRequired",
    "CodexWorkerConfig",
    "CodexWorkerError",
    "WorkerTaskRequest",
    "WorkerTurnResult",
]
