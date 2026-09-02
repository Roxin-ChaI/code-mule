"""Standard-library command-line grammar for the Boss interface."""

import argparse
from pathlib import Path

from code_mule.domain.enums import HumanResolutionStrategy


DEFAULT_STATE_FILE = Path(".code-mule/project-state.json")


def _state_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="project-state JSON file (default: .code-mule/project-state.json)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-mule")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show a sanitized traceback for failures",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize an IDLE project")
    init.add_argument("--project-id", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--workspace", type=Path, default=Path.cwd())
    _state_file(init)

    run = commands.add_parser("run", help="plan or continue project execution")
    run.add_argument("--objective")
    _state_file(run)

    status = commands.add_parser("status", help="show project status")
    _state_file(status)

    ask = commands.add_parser("ask", help="query project status read-only")
    ask.add_argument("question")
    _state_file(ask)

    change = commands.add_parser("change", help="submit or apply a Boss change")
    change.add_argument("request", nargs="?")
    change.add_argument("--apply", action="store_true")
    _state_file(change)

    pause = commands.add_parser("pause", help="pause at the control boundary")
    _state_file(pause)

    resume = commands.add_parser("resume", help="resume a Boss-paused project")
    _state_file(resume)

    inspect = commands.add_parser("inspect", help="inspect the pending human action")
    inspect.add_argument("--verbose", action="store_true")
    _state_file(inspect)

    approve = commands.add_parser("approve", help="approve one specific pending action")
    approve.add_argument("action_id")
    _state_file(approve)

    reject = commands.add_parser("reject", help="reject one specific pending action")
    reject.add_argument("action_id")
    _state_file(reject)

    resolve = commands.add_parser("resolve", help="resolve a non-approval action")
    resolve.add_argument("action_id")
    resolve.add_argument(
        "--strategy",
        required=True,
        choices=(
            HumanResolutionStrategy.RETRY_TASK.value,
            HumanResolutionStrategy.FAIL_PROJECT.value,
            HumanResolutionStrategy.ACKNOWLEDGE.value,
        ),
    )
    _state_file(resolve)
    return parser


__all__ = ["DEFAULT_STATE_FILE", "build_parser"]
