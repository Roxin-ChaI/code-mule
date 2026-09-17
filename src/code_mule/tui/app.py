"""Persistent terminal entry point: fixed Status / Activity / Boss panes.

The UI is a controller over the existing command layer.  It requires a TTY; a
non-TTY caller keeps the stable one-shot text output instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import sys
from typing import TextIO

from code_mule.cli.app import BossCliCommands
from code_mule.state.models import ProjectState

from .controller import PAGE_STEP, SCROLL_STEP, TerminalController
from .keys import KEY_NAMES, key_name
from .layout import compute_layout, render_screen, wrapped_height
from .snapshot import build_snapshot, status_lines


REFRESH_MILLISECONDS = 250
STATUS_SIZE = 8
BOSS_MIN_HEIGHT = 4


@dataclass(frozen=True)
class TerminalAvailability:
    """Whether the persistent UI can take over the terminal."""

    interactive: bool
    reason: str | None = None


def availability(stdin: TextIO, stdout: TextIO) -> TerminalAvailability:
    if not _isatty(stdin):
        return TerminalAvailability(False, "stdin is not a terminal")
    if not _isatty(stdout):
        return TerminalAvailability(False, "stdout is not a terminal")
    return TerminalAvailability(True)


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def fallback_lines(
    controller: TerminalController, *, rows: int = 24, cols: int = 80
) -> tuple[str, ...]:
    """Deterministic text frame for non-TTY callers.

    Text output has no fixed screen to preserve, so the Status and Boss panes
    are sized to their content and nothing important is clipped.
    """

    state = controller.project_state()
    controller.refresh_activity_from_events()
    layout = compute_layout(
        rows,
        cols,
        status_height=max(
            STATUS_SIZE, wrapped_height(status_lines(state) if state else (), cols)
        ),
        boss_height=max(BOSS_MIN_HEIGHT, 5),
    )
    snapshot = snapshot_for(
        controller,
        state,
        buffer=controller.buffered_input,
        activity_height=layout.activity_height,
    )
    layout = compute_layout(
        rows,
        cols,
        status_height=max(STATUS_SIZE, wrapped_height(snapshot.status, cols)),
        boss_height=max(BOSS_MIN_HEIGHT, wrapped_height(snapshot.boss, cols)),
    )
    snapshot = snapshot_for(
        controller,
        state,
        buffer=controller.buffered_input,
        activity_height=layout.activity_height,
    )
    return render_screen(
        layout,
        status=snapshot.status,
        activity=snapshot.activity,
        boss=snapshot.boss,
        indicator=snapshot.indicator,
    )


def snapshot_for(
    controller: TerminalController,
    state: ProjectState | None,
    *,
    buffer: str,
    activity_height: int,
):
    controller.refresh_activity_from_events()
    # The indicator occupies the first Activity row, so the window is one smaller.
    window_height = max(
        1, activity_height - (1 if activity_height >= 2 else 0)
    )
    entries = controller.activity.visible(window_height)
    lines = tuple(entry.text for entry in entries)
    return build_snapshot(
        state,
        lines,
        buffer=buffer,
        status_message=controller.state.status_message,
        key_debug=controller.key_debug,
        indicator=controller.activity.window_label(window_height),
        now=datetime.now(UTC),
    )


def run_terminal(
    controller: TerminalController,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the persistent terminal, or fall back to one stable text frame."""

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    verdict = availability(stdin, stdout)
    if not verdict.interactive:
        for line in fallback_lines(controller):
            print(line, file=stdout)
        print(f"persistent UI unavailable: {verdict.reason}", file=stdout)
        print("Run this command in a terminal for the persistent interface.", file=stdout)
        return 0
    return _run_curses(controller, stdout=stdout)


def _run_curses(controller: TerminalController, *, stdout: TextIO) -> int:
    import curses
    import locale

    locale.setlocale(locale.LC_ALL, "")

    def loop(stdscr) -> int:
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.timeout(REFRESH_MILLISECONDS)
        stdscr.keypad(True)
        while not controller.should_quit:
            rows, cols = stdscr.getmaxyx()
            _draw(stdscr, controller, rows=rows, cols=cols)
            key = _read_key(stdscr)
            if key is None:
                continue
            if not _handle_key(controller, key, rows=rows, cols=cols):
                continue
        return 0

    try:
        return curses.wrapper(loop)
    except KeyboardInterrupt:
        # Ctrl+C must never leave the project in a modified state.
        return 0


def _read_key(stdscr):
    try:
        return stdscr.get_wch()
    except Exception:
        return None


def _handle_key(controller: TerminalController, key, *, rows: int, cols: int) -> bool:
    import curses

    controller.note_key(key_name(key))
    activity_height = compute_layout(
        rows, cols, status_height=STATUS_SIZE, boss_height=BOSS_MIN_HEIGHT
    ).activity_height
    if key == curses.KEY_RESIZE:
        controller.state.dirty = True
        return True
    if isinstance(key, str) and key == "\x03":
        controller.request_quit()
        return True
    if isinstance(key, str) and key == "\x0c":
        controller.state.dirty = True
        return True
    if key in (curses.KEY_UP,):
        controller.scroll(SCROLL_STEP, visible=activity_height)
        return True
    if key in (curses.KEY_DOWN,):
        controller.scroll(-SCROLL_STEP, visible=activity_height)
        return True
    if key in (curses.KEY_PPAGE,):
        controller.scroll(PAGE_STEP, visible=activity_height)
        return True
    if key in (curses.KEY_NPAGE,):
        controller.scroll(-PAGE_STEP, visible=activity_height)
        return True
    if key in (curses.KEY_END,):
        controller.scroll_to_latest()
        return True
    if key in (curses.KEY_ENTER, "\n", "\r"):
        controller.submit_async(visible=activity_height)
        return True
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        controller.backspace()
        return True
    if isinstance(key, str) and key.isprintable():
        controller.insert(key)
        return True
    return False


def _draw(stdscr, controller: TerminalController, *, rows: int, cols: int) -> None:
    state = controller.project_state()
    layout = compute_layout(
        rows, cols, status_height=STATUS_SIZE, boss_height=BOSS_MIN_HEIGHT
    )
    snapshot = snapshot_for(
        controller,
        state,
        buffer=controller.buffered_input,
        activity_height=layout.activity_height,
    )
    lines = render_screen(
        layout,
        status=snapshot.status,
        activity=snapshot.activity,
        boss=snapshot.boss,
        indicator=snapshot.indicator,
    )
    # Repaint every row of every pane so a resize never leaves a ghost cell.
    stdscr.erase()
    for index, line in enumerate(lines[:rows]):
        try:
            stdscr.addstr(index, 0, line[:cols])
        except Exception:
            continue
    try:
        stdscr.refresh()
    except Exception:
        pass
    controller.state.dirty = False


__all__ = [
    "BOSS_MIN_HEIGHT",
    "REFRESH_MILLISECONDS",
    "STATUS_SIZE",
    "TerminalAvailability",
    "availability",
    "fallback_lines",
    "run_terminal",
]
