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
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show internal IDs and raw control values",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-mule")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show a sanitized traceback for failures",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check environment and workspace readiness")
    _state_file(doctor)

    start = commands.add_parser(
        "start",
        help="initialize and start a new project from the current directory",
    )
    start.add_argument("--objective")
    _state_file(start)

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

    diagnose = commands.add_parser(
        "diagnose", help="explain blockers, recoverability, and the next Boss action"
    )
    _state_file(diagnose)

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

    recover = commands.add_parser(
        "recover", help="recover from a persisted execution boundary"
    )
    _state_file(recover)

    stop = commands.add_parser("stop", help="cancel the project at a safe boundary")
    _state_file(stop)

    inspect = commands.add_parser("inspect", help="inspect the pending human action")
    _state_file(inspect)

    approve = commands.add_parser("approve", help="approve one specific pending action")
    approve.add_argument("action_id")
    _state_file(approve)

    reject = commands.add_parser("reject", help="reject one specific pending action")
    reject.add_argument("action_id")
    _state_file(reject)

    answer = commands.add_parser("answer", help="answer one Worker input request")
    answer.add_argument("action_id")
    answer.add_argument("answer")
    _state_file(answer)

    resolve = commands.add_parser("resolve", help="resolve a non-approval action")
    resolve.add_argument("action_id")
    resolve.add_argument(
        "--strategy",
        required=True,
        choices=(
            HumanResolutionStrategy.RETRY_TASK.value,
            HumanResolutionStrategy.RETRY_PLANNING.value,
            HumanResolutionStrategy.FAIL_PROJECT.value,
            HumanResolutionStrategy.ACKNOWLEDGE.value,
        ),
        help="use one strategy listed by code-mule inspect",
    )
    _state_file(resolve)

    chat = commands.add_parser("chat", help="start an interactive Boss conversation")
    _state_file(chat)
    return parser


__all__ = ["DEFAULT_STATE_FILE", "build_parser"]
