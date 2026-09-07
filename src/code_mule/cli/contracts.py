"""Typed public contracts for the Boss CLI boundary."""

from dataclasses import dataclass
from enum import IntEnum


class CliExitCode(IntEnum):
    SUCCESS = 0
    INVALID_USAGE = 2
    INVALID_PROJECT_STATE = 3
    HUMAN_ACTION_REQUIRED = 4
    PROVIDER_OR_WORKER_FAILURE = 5
    ENVIRONMENT_CHECK_FAILED = 6


class CliError(RuntimeError):
    """A safe user-facing CLI failure with a deterministic exit code."""

    exit_code = CliExitCode.PROVIDER_OR_WORKER_FAILURE

    def __init__(self, public_message: str) -> None:
        if public_message == "":
            raise ValueError("public_message must not be empty")
        super().__init__(public_message)
        self.public_message = public_message


class InvalidCliProjectState(CliError):
    exit_code = CliExitCode.INVALID_PROJECT_STATE


class CliUsageError(CliError):
    exit_code = CliExitCode.INVALID_USAGE


class CliHumanActionRequired(CliError):
    exit_code = CliExitCode.HUMAN_ACTION_REQUIRED


class CliExecutionFailure(CliError):
    exit_code = CliExitCode.PROVIDER_OR_WORKER_FAILURE


class CliProjectAlreadyRunning(InvalidCliProjectState):
    """Raised before side effects when another local owner holds execution."""


class CliRecoveryRequired(CliHumanActionRequired):
    """Raised when a stale execution cannot be resumed safely."""


@dataclass(frozen=True)
class CliCommandResult:
    exit_code: CliExitCode
    output: tuple[str, ...] = ()


__all__ = [
    "CliCommandResult",
    "CliError",
    "CliExecutionFailure",
    "CliExitCode",
    "CliHumanActionRequired",
    "CliProjectAlreadyRunning",
    "CliRecoveryRequired",
    "CliUsageError",
    "InvalidCliProjectState",
]
