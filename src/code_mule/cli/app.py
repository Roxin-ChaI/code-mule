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
    CliUsageError,
    InvalidCliProjectState,
)
from .parser import build_parser


class BossCliCommands(Protocol):
    def init_project(self, project_id: str, name: str, workspace: Path, verbose: bool = False) -> CliCommandResult: ...
    def run(self, objective: str | None, verbose: bool = False) -> CliCommandResult: ...
    def status(self, verbose: bool = False) -> CliCommandResult: ...
    def ask(self, question: str, verbose: bool = False) -> CliCommandResult: ...
    def change(self, request: str, verbose: bool = False) -> CliCommandResult: ...
    def apply_change(self, verbose: bool = False) -> CliCommandResult: ...
    def pause(self, verbose: bool = False) -> CliCommandResult: ...
    def resume(self, verbose: bool = False) -> CliCommandResult: ...
    def inspect(self, verbose: bool) -> CliCommandResult: ...
    def approve(self, action_id: str, verbose: bool = False) -> CliCommandResult: ...
    def reject(self, action_id: str, verbose: bool = False) -> CliCommandResult: ...
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


def _dispatch(
    commands: BossCliCommands, arguments: object, input_stream: TextIO
) -> CliCommandResult:
    command = getattr(arguments, "command")
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
    if command == "inspect":
        return commands.inspect(getattr(arguments, "verbose"))
    if command == "approve":
        return commands.approve(getattr(arguments, "action_id"), getattr(arguments, "verbose"))
    if command == "reject":
        return commands.reject(getattr(arguments, "action_id"), getattr(arguments, "verbose"))
    if command == "resolve":
        return commands.resolve(
            getattr(arguments, "action_id"),
            HumanResolutionStrategy(getattr(arguments, "strategy")),
            getattr(arguments, "verbose"),
        )
    if command == "chat":
        return commands.chat(input_stream, getattr(arguments, "verbose"))
    raise CliUsageError("unsupported command")


def _print_error(error: CliError, stream: TextIO) -> None:
    if isinstance(error, CliHumanActionRequired):
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
    commands = composition_factory(
        arguments.state_file,
        os.environ if environment is None else environment,
        output,
        errors,
    )
    try:
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

    for line in result.output:
        print(line, file=output)
    return int(result.exit_code)


__all__ = ["BossCliCommands", "CompositionFactory", "main"]
