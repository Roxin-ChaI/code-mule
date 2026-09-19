"""CLI process boundary and safe error presentation."""

from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import sys
import traceback
from typing import Protocol, TextIO

from code_mule.domain.enums import HumanResolutionStrategy

from .contracts import (
    CliCommandResult,
    CliError,
    CliExecutionFailure,
    CliExitCode,
    CliHumanActionRequired,
    CliProjectAlreadyRunning,
    CliRecoveryRequired,
    CliStateCompatibilityError,
    CliUsageError,
    InvalidCliProjectState,
)
from .compatibility import command_requires_state_preflight, inspect_state_file_schema
from .parser import build_parser
from code_mule.presentation.terminal import TerminalDashboard, DashboardSection
from code_mule.state.serialization import StateSchemaCompatibilityError


class BossCliCommands(Protocol):
    def doctor(self, verbose: bool = False) -> CliCommandResult: ...
    def ui(self, demo: bool = False, verbose: bool = False) -> CliCommandResult: ...
    def start(self, objective: str | None, verbose: bool = False) -> CliCommandResult: ...
    def init_project(self, project_id: str, name: str, workspace: Path, verbose: bool = False) -> CliCommandResult: ...
    def run(self, objective: str | None, verbose: bool = False) -> CliCommandResult: ...
    def status(self, verbose: bool = False) -> CliCommandResult: ...
    def deliverable(self, verbose: bool = False) -> CliCommandResult: ...
    def launch(self, verbose: bool = False) -> CliCommandResult: ...
    def app_status(self, verbose: bool = False) -> CliCommandResult: ...
    def stop_app(self, verbose: bool = False) -> CliCommandResult: ...
    def diagnose(self, verbose: bool = False) -> CliCommandResult: ...
    def ask(self, question: str, verbose: bool = False) -> CliCommandResult: ...
    def change(self, request: str, verbose: bool = False) -> CliCommandResult: ...
    def apply_change(self, verbose: bool = False) -> CliCommandResult: ...
    def pause(self, verbose: bool = False) -> CliCommandResult: ...
    def resume(self, verbose: bool = False) -> CliCommandResult: ...
    def recover(self, verbose: bool = False) -> CliCommandResult: ...
    def stop(self, verbose: bool = False) -> CliCommandResult: ...
    def inspect(self, verbose: bool) -> CliCommandResult: ...
    def approve(self, action_id: str, verbose: bool = False) -> CliCommandResult: ...
    def reject(self, action_id: str, verbose: bool = False) -> CliCommandResult: ...
    def answer(self, action_id: str, answer: str, verbose: bool = False) -> CliCommandResult: ...
    def resolve(self, action_id: str, strategy: HumanResolutionStrategy, verbose: bool = False) -> CliCommandResult: ...
    def chat(self, input_stream: TextIO, verbose: bool = False) -> CliCommandResult: ...


CompositionFactory = Callable[[Path, Mapping[str, str], TextIO, TextIO], BossCliCommands]


def _production_factory(
    state_file: Path,
    environment: Mapping[str, str],
    stdout: TextIO,
    stderr: TextIO,
) -> BossCliCommands:
    from .composition import ProductionCliComposition

    return ProductionCliComposition(
        state_file,
        environment=environment,
        stdout=stdout,
        stderr=stderr,
    )


def dispatch_arguments(
    commands: BossCliCommands, arguments: object, input_stream: TextIO
) -> CliCommandResult:
    command = getattr(arguments, "command")
    if command == "doctor":
        return commands.doctor(getattr(arguments, "verbose"))
    if command == "ui":
        return commands.ui(
            getattr(arguments, "demo", False), getattr(arguments, "verbose")
        )
    if command == "start":
        return commands.start(
            getattr(arguments, "objective"),
            getattr(arguments, "verbose"),
        )
    if command == "init":
        return commands.init_project(
            getattr(arguments, "project_id"),
            getattr(arguments, "name"),
            getattr(arguments, "workspace"),
            getattr(arguments, "verbose"),
        )
    if command == "run":
        return commands.run(getattr(arguments, "objective"), getattr(arguments, "verbose"))
    if command == "status":
        return commands.status(getattr(arguments, "verbose"))
    if command == "deliverable":
        return commands.deliverable(getattr(arguments, "verbose"))
    if command == "launch":
        return commands.launch(getattr(arguments, "verbose"))
    if command == "app-status":
        return commands.app_status(getattr(arguments, "verbose"))
    if command == "stop-app":
        return commands.stop_app(getattr(arguments, "verbose"))
    if command == "diagnose":
        return commands.diagnose(getattr(arguments, "verbose"))
    if command == "ask":
        return commands.ask(getattr(arguments, "question"), getattr(arguments, "verbose"))
    if command == "change":
        request = getattr(arguments, "request")
        apply = getattr(arguments, "apply")
        if apply and request is not None:
            raise CliUsageError("change accepts either a request or --apply, not both")
        if not apply and request is None:
            raise CliUsageError("change requires a request or --apply")
        return commands.apply_change(getattr(arguments, "verbose")) if apply else commands.change(request, getattr(arguments, "verbose"))
    if command == "pause":
        return commands.pause(getattr(arguments, "verbose"))
    if command == "resume":
        return commands.resume(getattr(arguments, "verbose"))
    if command == "recover":
        return commands.recover(getattr(arguments, "verbose"))
    if command == "stop":
        return commands.stop(getattr(arguments, "verbose"))
    if command == "inspect":
        return commands.inspect(getattr(arguments, "verbose"))
    if command == "approve":
        return commands.approve(getattr(arguments, "action_id"), getattr(arguments, "verbose"))
    if command == "reject":
        return commands.reject(getattr(arguments, "action_id"), getattr(arguments, "verbose"))
    if command == "answer":
        return commands.answer(
            getattr(arguments, "action_id"),
            getattr(arguments, "answer"),
            getattr(arguments, "verbose"),
        )
    if command == "resolve":
        return commands.resolve(
            getattr(arguments, "action_id"),
            HumanResolutionStrategy(getattr(arguments, "strategy")),
            getattr(arguments, "verbose"),
        )
    if command == "chat":
        return commands.chat(input_stream, getattr(arguments, "verbose"))
    raise CliUsageError("unsupported command")


# Historical private name; the persistent terminal uses the public spelling.
_dispatch = dispatch_arguments


def _print_error(error: CliError, stream: TextIO) -> None:
    if isinstance(error, CliStateCompatibilityError):
        compatibility = error.compatibility
        title = (
            "CLI UPDATE REQUIRED"
            if compatibility.code.value == "CLI_TOO_OLD"
            else "STATE SCHEMA ERROR"
        )
        next_command = "code-mule doctor --verbose"
    elif isinstance(error, CliProjectAlreadyRunning):
        title = "PROJECT ALREADY RUNNING"
        next_command = "code-mule status"
    elif isinstance(error, CliRecoveryRequired):
        title = "RECOVERY REQUIRED"
        next_command = "code-mule inspect"
    elif isinstance(error, CliHumanActionRequired):
        title = "ACTION REQUIRED"
        next_command = "code-mule inspect"
    elif isinstance(error, CliExecutionFailure):
        title = "PROVIDER / WORKER ERROR"
        next_command = "code-mule status"
    elif isinstance(error, InvalidCliProjectState):
        title = "PROJECT STATE ERROR"
        next_command = "code-mule status"
    else:
        title = "COMMAND ERROR"
        next_command = "code-mule --help"
    terminal = TerminalDashboard.for_stream(stream)
    if terminal.interactive:
        if isinstance(error, CliRecoveryRequired):
            title = "RECOVERY BLOCKED"
        for line in terminal.render((DashboardSection(title, (
            "Why            " + error.public_message,
            "Risk           Safety requirements have not been satisfied.",
            "No unsafe operation was performed.",
            "", "Next           " + next_command)),)):
            print(line, file=stream)
        return
    print(title, file=stream)
    print(file=stream)
    print(error.public_message, file=stream)
    print(file=stream)
    print("No unsafe operation was performed.", file=stream)
    print(file=stream)
    print("Next:", file=stream)
    print(f"  {next_command}", file=stream)


def main(
    argv: Sequence[str] | None = None,
    *,
    composition_factory: CompositionFactory = _production_factory,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    inputs = stdin or sys.stdin
    arguments = build_parser().parse_args(argv)
    try:
        if (
            command_requires_state_preflight(arguments)
            and arguments.state_file.exists()
        ):
            try:
                inspect_state_file_schema(arguments.state_file)
            except StateSchemaCompatibilityError as error:
                compatibility = error.compatibility
                schema = compatibility.project_schema
                message = (
                    f"{compatibility.code.value}: project schema "
                    f"{'unknown' if schema is None else schema}; CLI supports "
                    f"{compatibility.supported_min}..{compatibility.supported_max}."
                )
                raise CliStateCompatibilityError(
                    message, compatibility=compatibility
                ) from error
        commands = composition_factory(
            arguments.state_file,
            os.environ if environment is None else environment,
            output,
            errors,
        )
        result = _dispatch(commands, arguments, inputs)
    except CliError as error:
        _print_error(error, errors)
        if arguments.debug:
            traceback.print_exception(
                type(error),
                RuntimeError(error.public_message),
                error.__traceback__,
                file=errors,
            )
        return int(error.exit_code)
    except BaseException as error:
        safe_message = "The operation could not be completed safely."
        print("UNEXPECTED ERROR", file=errors)
        print(file=errors)
        print(safe_message, file=errors)
        print(file=errors)
        print("Next:", file=errors)
        print("  code-mule status", file=errors)
        if arguments.debug:
            traceback.print_exception(
                type(error),
                RuntimeError(safe_message),
                error.__traceback__,
                file=errors,
            )
        return int(CliExitCode.PROVIDER_OR_WORKER_FAILURE)

    lines = result.output
    terminal = TerminalDashboard.for_stream(output)
    if not any(line.startswith("┌") for line in lines):
        lines = terminal.legacy(lines)
    for line in lines:
        print(line, file=output)
    return int(result.exit_code)


__all__ = ["BossCliCommands", "CompositionFactory", "main"]
