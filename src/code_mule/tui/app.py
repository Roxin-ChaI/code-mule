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
from .keys import (
    ESCAPE,
    KEY_NAMES,
    MAX_ESCAPE_LENGTH,
    key_name,
    resolve_sequence,
    sequence_table,
)
from .layout import compute_layout, render_screen, wrapped_height
from .snapshot import build_snapshot, status_lines


REFRESH_MILLISECONDS = 250
STATUS_SIZE = 8
BOSS_MIN_HEIGHT = 4
# A split escape sequence arrives within a few milliseconds; this bound keeps
# the read responsive while never blocking on a bare ESC keypress.
ESCAPE_AGGREGATE_MILLISECONDS = 30


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
    indicator = controller.activity.window_label(window_height)
    key_display = controller.key_display
    if key_display is not None:
        # Demo/debug readout lives beside the Activity status line, so it never
        # competes with the Boss input row.
        indicator = f"{indicator} · Key: {key_display}"
    return build_snapshot(
        state,
        lines,
        buffer=buffer,
        status_message=controller.state.status_message,
        indicator=indicator,
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
        try:
            curses.curs_set(0)
        except curses.error:
            # Hiding the cursor is cosmetic. Some otherwise usable terminals
            # reject this capability; the persistent session must stay open.
            pass
        stdscr.nodelay(False)
        stdscr.timeout(REFRESH_MILLISECONDS)
        stdscr.keypad(True)
        while not controller.should_quit:
            rows, cols = stdscr.getmaxyx()
            _draw(stdscr, controller, rows=rows, cols=cols)
            name = read_key_event(stdscr)
            if name is None:
                continue
            if not _handle_key(controller, name, rows=rows, cols=cols):
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


def read_key_event(stdscr, *, read_tail=None, table=None) -> str | None:
    """Read one key and resolve it to a safe name, aggregating escape tails.

    macOS Terminal delivers ``Fn+Right`` as the terminfo `kend` sequence
    (``ESC O F``).  A single ``get_wch`` call can return those bytes one at a
    time, so a lone ESC is followed by a bounded tail read before resolution;
    otherwise a valid End keypress degrades into ``UNKNOWN`` plus stray text.
    """

    key = _read_key(stdscr)
    if key is None:
        return None
    if isinstance(key, str) and key.startswith(ESCAPE):
        tail_reader = read_tail if read_tail is not None else _read_escape_tail
        resolved_table = table or sequence_table()
        remainder = key[1:]
        if remainder == "":
            remainder = tail_reader(stdscr)
        elif (
            resolved_table.resolve(ESCAPE + remainder) is None
            and resolved_table.is_prefix(ESCAPE + remainder)
        ):
            # A read may hand back part of the sequence; collect the rest.
            remainder += tail_reader(stdscr)
        return resolve_sequence(ESCAPE + remainder, resolved_table)
    return key_name(key)


def _read_escape_tail(stdscr, *, table=None) -> str:
    """Bounded, prefix-aware collection of one escape sequence's remainder."""

    import curses

    table = table or sequence_table()
    collected = ""
    previous_timeout = None
    try:
        stdscr.timeout(ESCAPE_AGGREGATE_MILLISECONDS)
    except Exception:
        previous_timeout = None
    try:
        while len(collected) < MAX_ESCAPE_LENGTH:
            key = _read_key(stdscr)
            if key is None or not isinstance(key, str):
                break
            if key == ESCAPE:
                break
            collected += key
            sequence = ESCAPE + collected
            if table.resolve(sequence) is not None:
                break
            if not table.is_prefix(sequence):
                break
    finally:
        if previous_timeout is None:
            try:
                stdscr.timeout(REFRESH_MILLISECONDS)
            except Exception:
                pass
        else:
            try:
                stdscr.timeout(previous_timeout)
            except Exception:
                pass
    return collected


def _handle_key(controller: TerminalController, key, *, rows: int, cols: int) -> bool:
    import curses

    # Accept either a resolved name (from read_key_event) or a raw curses key,
    # so the aggregation layer and direct callers share one behaviour.
    name = key if isinstance(key, str) and key in KEY_NAMES else key_name(key)
    controller.note_key(name)
    activity_height = compute_layout(
        rows, cols, status_height=STATUS_SIZE, boss_height=BOSS_MIN_HEIGHT
    ).activity_height
    if name == "KEY_RESIZE":
        controller.state.dirty = True
        return True
    if name == "CTRL_C":
        controller.request_quit()
        return True
    if name == "CTRL_L":
        controller.state.dirty = True
        return True
    if name == "KEY_UP":
        controller.scroll(SCROLL_STEP, visible=activity_height)
        return True
    if name == "KEY_DOWN":
        controller.scroll(-SCROLL_STEP, visible=activity_height)
        return True
    if name == "KEY_PPAGE":
        controller.scroll(PAGE_STEP, visible=activity_height)
        return True
    if name == "KEY_NPAGE":
        controller.scroll(-PAGE_STEP, visible=activity_height)
        return True
    if name == "KEY_END":
        controller.scroll_to_latest()
        return True
    if name in ("KEY_ENTER", "ENTER"):
        controller.submit_async(visible=activity_height)
        return True
    if name == "BACKSPACE":
        controller.backspace()
        return True
    if name == "TEXT" and isinstance(key, str):
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
