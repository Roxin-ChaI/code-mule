"""CLI process boundary and safe error presentation."""

from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import sys
import traceback
from typing import Protocol, TextIO

from code_mule.domain.enums import HumanResolutionStrategy

from .contracts import CliCommandResult, CliError, CliExitCode, CliUsageError
from .parser import build_parser


class BossCliCommands(Protocol):
    def init_project(self, project_id: str, name: str, workspace: Path) -> CliCommandResult: ...
    def run(self, objective: str | None) -> CliCommandResult: ...
    def status(self) -> CliCommandResult: ...
    def ask(self, question: str) -> CliCommandResult: ...
    def change(self, request: str) -> CliCommandResult: ...
    def apply_change(self) -> CliCommandResult: ...
    def pause(self) -> CliCommandResult: ...
    def resume(self) -> CliCommandResult: ...
    def inspect(self, verbose: bool) -> CliCommandResult: ...
    def approve(self, action_id: str) -> CliCommandResult: ...
    def reject(self, action_id: str) -> CliCommandResult: ...
    def resolve(self, action_id: str, strategy: HumanResolutionStrategy) -> CliCommandResult: ...


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


def _dispatch(commands: BossCliCommands, arguments: object) -> CliCommandResult:
    command = getattr(arguments, "command")
    if command == "init":
        return commands.init_project(
            getattr(arguments, "project_id"),
            getattr(arguments, "name"),
            getattr(arguments, "workspace"),
        )
    if command == "run":
        return commands.run(getattr(arguments, "objective"))
    if command == "status":
        return commands.status()
    if command == "ask":
        return commands.ask(getattr(arguments, "question"))
    if command == "change":
        request = getattr(arguments, "request")
        apply = getattr(arguments, "apply")
        if apply and request is not None:
            raise CliUsageError("change accepts either a request or --apply, not both")
        if not apply and request is None:
            raise CliUsageError("change requires a request or --apply")
        return commands.apply_change() if apply else commands.change(request)
    if command == "pause":
        return commands.pause()
    if command == "resume":
        return commands.resume()
    if command == "inspect":
        return commands.inspect(getattr(arguments, "verbose"))
    if command == "approve":
        return commands.approve(getattr(arguments, "action_id"))
    if command == "reject":
        return commands.reject(getattr(arguments, "action_id"))
    if command == "resolve":
        return commands.resolve(
            getattr(arguments, "action_id"),
            HumanResolutionStrategy(getattr(arguments, "strategy")),
        )
    raise CliUsageError("unsupported command")


def main(
    argv: Sequence[str] | None = None,
    *,
    composition_factory: CompositionFactory = _production_factory,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    arguments = build_parser().parse_args(argv)
    commands = composition_factory(
        arguments.state_file,
        os.environ if environment is None else environment,
        output,
        errors,
    )
    try:
        result = _dispatch(commands, arguments)
    except CliError as error:
        print(f"ERROR: {error.public_message}", file=errors)
        if arguments.debug:
            traceback.print_exception(
                type(error),
                RuntimeError(error.public_message),
                error.__traceback__,
                file=errors,
            )
        return int(error.exit_code)
    except BaseException as error:
        safe_message = f"operation failed ({type(error).__name__})"
        print(f"ERROR: {safe_message}", file=errors)
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
