"""Persistent-terminal controller.

The controller is presentation only: it parses Boss input with the same CLI
grammar, dispatches it through the existing command layer, and renders persisted
state.  It never mutates ProjectState, never approves a HumanAction by itself,
and never starts a Worker on its own.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import io
import shlex
import threading
from typing import TextIO

from code_mule.cli.app import BossCliCommands, dispatch_arguments
from code_mule.cli.contracts import CliCommandResult, CliError, CliExitCode
from code_mule.cli.parser import build_parser
from code_mule.progress.contracts import ProgressEvent
from code_mule.state.models import ProjectState

from .activity import ActivityKind, ActivityLog
from .keys import KEY_NAMES


SCROLL_STEP = 1
PAGE_STEP = 5

# Boss commands whose trailing text is one free-form argument; the line is
# rebuilt so "change add a health endpoint" works as typed.
_FREE_TEXT_COMMANDS = frozenset({"change", "ask"})
_IDENTIFIER_THEN_FREE_TEXT = frozenset({"answer"})


@dataclass
class ControllerState:
    """Mutable view state owned by the controller (never business truth)."""

    buffer: str = ""
    status_message: str | None = None
    busy: bool = False
    should_quit: bool = False
    dirty: bool = True
    last_exit_code: int | None = None
    last_key: str | None = None
    key_debug: bool = False


class TerminalController:
    """Owns input, scrolling, and dispatch for one persistent session."""

    def __init__(
        self,
        commands: BossCliCommands,
        load_state: Callable[[], ProjectState | None],
        *,
        input_stream: TextIO | None = None,
    ) -> None:
        self._commands = commands
        self._load_state = load_state
        self._input_stream = input_stream or io.StringIO("")
        self.activity = ActivityLog()
        self.state = ControllerState()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._seen_events: set[str] = set()

    # -- state ---------------------------------------------------------------

    @property
    def buffered_input(self) -> str:
        return self.state.buffer

    @property
    def busy(self) -> bool:
        return self.state.busy

    @property
    def should_quit(self) -> bool:
        return self.state.should_quit

    def project_state(self) -> ProjectState | None:
        """Read persisted truth; a missing project is not an error here."""

        try:
            return self._load_state()
        except Exception:
            return None

    def refresh_activity_from_events(self) -> bool:
        """Append newly persisted events; deduplicated, bounded, no payload."""

        state = self.project_state()
        if state is None:
            return False
        changed = False
        for event in state.events:
            if event.id in self._seen_events:
                continue
            self._seen_events.add(event.id)
            changed |= self.activity.append(
                event.timestamp,
                ActivityKind.INFO,
                event.event_type,
                key=f"state:{event.id}",
            )
        return changed

    def record_progress_event(self, event: ProgressEvent) -> bool:
        """Feed one live progress event from the running command."""

        with self._lock:
            return self.activity.record_event(event)

    # -- input ---------------------------------------------------------------

    def insert(self, text: str) -> None:
        self.state.buffer += text
        self.state.status_message = None
        self.state.dirty = True

    def backspace(self) -> None:
        self.state.buffer = self.state.buffer[:-1]
        self.state.dirty = True

    def scroll(self, lines: int, *, visible: int) -> None:
        with self._lock:
            self.activity.scroll(lines, visible=visible)
        self.state.dirty = True

    def scroll_to_latest(self) -> None:
        with self._lock:
            self.activity.scroll_to_latest()
        self.state.dirty = True

    def request_quit(self) -> None:
        """Ctrl+C leaves the UI without touching project state."""

        self.state.should_quit = True
        self.state.status_message = "Leaving the persistent terminal. Project state was not modified."
        self.state.dirty = True

    def note_key(self, name: str) -> None:
        """Record one resolved, safe key name for demo/debug display."""

        if name not in KEY_NAMES:
            name = "UNKNOWN"
        self.state.last_key = name
        self.state.dirty = True

    @property
    def key_debug(self) -> str | None:
        """The last key name, but only while demo/debug display is enabled."""

        return self.state.last_key if self.state.key_debug else None

    @property
    def key_display(self) -> str | None:
        """Readout next to the Activity status line, or None when disabled.

        Always present while demo debugging is on: an untouched session shows an
        em dash so the Boss can tell "no key yet" from "readout missing".
        """

        if not self.state.key_debug:
            return None
        return self.state.last_key or "—"

    def clear_buffer(self) -> None:
        self.state.buffer = ""
        self.state.dirty = True

    def submit(self, *, visible: int = 1) -> CliCommandResult | None:
        """Dispatch the buffered line through the existing command layer."""

        line = self.state.buffer.strip()
        self.state.buffer = ""
        self.state.dirty = True
        if line == "":
            return None
        return self.dispatch(line, visible=visible)

    def dispatch(self, line: str, *, visible: int = 1) -> CliCommandResult:
        """Parse and run one Boss line exactly as the one-shot CLI would."""

        if line.strip().lower() in {"exit", "quit"}:
            self.request_quit()
            result = CliCommandResult(
                CliExitCode.SUCCESS,
                ("Leaving the persistent terminal. Project state was not modified.",),
            )
            self._record_result(line, result, visible=visible)
            return result
        result = self._run(line)
        self._record_result(line, result, visible=visible)
        return result

    def submit_async(self, *, visible: int = 1) -> None:
        """Run one command off the UI thread so activity keeps rendering."""

        line = self.state.buffer.strip()
        self.state.buffer = ""
        if line == "":
            return
        if self.state.busy:
            with self._lock:
                self.activity.append(
                    datetime.now().astimezone(),
                    ActivityKind.FAILURE,
                    "A command is already running.",
                )
            self.state.dirty = True
            return
        self.state.busy = True
        self.state.dirty = True

        def worker() -> None:
            try:
                result = self._run(line)
            except Exception as error:  # noqa: BLE001 - reported, never hidden
                result = CliCommandResult(
                    CliExitCode.PROVIDER_OR_WORKER_FAILURE,
                    ("The command could not be completed safely.", type(error).__name__),
                )
            finally:
                self.state.busy = False
            self._record_result(line, result, visible=visible)

        self._thread = threading.Thread(target=worker, name="code-mule-tui-command", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # -- internals -----------------------------------------------------------

    def _run(self, line: str) -> CliCommandResult:
        line = _normalize_command(line)
        try:
            arguments = build_parser().parse_args(shlex.split(line))
        except SystemExit as error:  # argparse rejects with exit code 2
            return CliCommandResult(
                CliExitCode.INVALID_USAGE,
                (f"Invalid command: {line}", str(error)),
            )
        except ValueError as error:
            return CliCommandResult(CliExitCode.INVALID_USAGE, (f"Invalid command: {error}",))
        try:
            return dispatch_arguments(self._commands, arguments, self._input_stream)
        except CliError as error:
            return CliCommandResult(error.exit_code, (error.public_message,))
        except Exception as error:  # noqa: BLE001 - surfaced, not swallowed
            return CliCommandResult(
                CliExitCode.PROVIDER_OR_WORKER_FAILURE,
                ("The command could not be completed safely.", type(error).__name__),
            )

    def _record_result(
        self, line: str, result: CliCommandResult, *, visible: int
    ) -> None:
        timestamp = datetime.now().astimezone()
        with self._lock:
            self.activity.append(timestamp, ActivityKind.COMMAND, f"me> {line}")
            for output in result.output[:40]:
                self.activity.append(timestamp, ActivityKind.RESPONSE, output)
        self.state.last_exit_code = int(result.exit_code)
        self.state.status_message = (
            "ok" if result.exit_code == CliExitCode.SUCCESS
            else f"exit {int(result.exit_code)}"
        )
        self.state.dirty = True
        self.activity.scroll_to_latest()


def _normalize_command(line: str) -> str:
    """Quote the free-form tail of commands that take natural language."""

    stripped = line.strip()
    if stripped == "":
        return stripped
    head, _, tail = stripped.partition(" ")
    tail = tail.strip()
    if tail == "":
        return stripped
    if head in _FREE_TEXT_COMMANDS:
        return f"{head} {shlex.quote(tail)}"
    if head in _IDENTIFIER_THEN_FREE_TEXT:
        action_id, _, rest = tail.partition(" ")
        rest = rest.strip()
        return (
            stripped
            if rest == ""
            else f"{head} {shlex.quote(action_id)} {shlex.quote(rest)}"
        )
    return stripped


__all__ = [
    "ControllerState",
    "PAGE_STEP",
    "SCROLL_STEP",
    "TerminalController",
    "_normalize_command",
]
