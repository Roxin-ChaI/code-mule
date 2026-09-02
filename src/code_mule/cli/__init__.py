"""Boss CLI public contracts."""

from .contracts import (
    CliCommandResult,
    CliError,
    CliExecutionFailure,
    CliExitCode,
    CliHumanActionRequired,
    CliProjectAlreadyRunning,
    CliRecoveryRequired,
    CliUsageError,
    InvalidCliProjectState,
)
from .parser import DEFAULT_STATE_FILE, build_parser
from .app import main

__all__ = [
    "CliCommandResult",
    "CliError",
    "CliExecutionFailure",
    "CliExitCode",
    "CliHumanActionRequired",
    "CliProjectAlreadyRunning",
    "CliRecoveryRequired",
    "CliUsageError",
    "DEFAULT_STATE_FILE",
    "InvalidCliProjectState",
    "build_parser",
    "main",
]
