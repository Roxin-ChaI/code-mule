"""Fixed three-layer terminal layout with cell-accurate, resize-safe painting.

Pure functions only: given a size and already-projected pane content, produce
exactly ``rows`` lines that each fit ``cols`` display cells.  No I/O, no curses,
so the geometry is fully testable headlessly.
"""

from dataclasses import dataclass
from enum import StrEnum

from code_mule.presentation.terminal import display_width, wrap_cells


MIN_ROWS_FOR_THREE_PANES = 7
MIN_PANE_HEIGHT = 1
RULE = "\u2500"


class Pane(StrEnum):
    """The three fixed regions of the persistent terminal."""

    STATUS = "status"
    ACTIVITY = "activity"
    BOSS = "boss"


@dataclass(frozen=True)
class ScreenLayout:
    """Resolved geometry for one terminal size."""

    rows: int
    cols: int
    status_height: int
    activity_height: int
    boss_height: int
    ruled: bool

    @property
    def total(self) -> int:
        rules = 2 if self.ruled else 0
        return (
            self.status_height
            + self.activity_height
            + self.boss_height
            + rules
        )

    def pane_for(self, line: int) -> Pane:
        """Which pane owns one zero-based screen line."""

        offset = 0
        if line < self.status_height:
            return Pane.STATUS
        offset += self.status_height
        if self.ruled:
            if line == offset:
                return Pane.STATUS
            offset += 1
        if line < offset + self.activity_height:
            return Pane.ACTIVITY
        offset += self.activity_height
        if self.ruled and line == offset:
            return Pane.BOSS
        return Pane.BOSS


def _fit(lines: tuple[str, ...], height: int, width: int, *, top: bool) -> tuple[str, ...]:
    """Wrap, clip, and pad content to exactly ``height`` lines of ``width``.

    ``top=False`` anchors the *tail*: the newest activity line and the Boss
    input prompt must survive truncation, so overflow drops the oldest lines.
    """

    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_cells(line, width) or [""])
    if len(wrapped) > height:
        wrapped = wrapped[:height] if top else wrapped[-height:]
    elif len(wrapped) < height:
        pad = [""] * (height - len(wrapped))
        wrapped = wrapped + pad if top else pad + wrapped
    return tuple(_pad(line, width) for line in wrapped)


def _pad(line: str, width: int) -> str:
    clipped = _clip_cells(line, width)
    return clipped + " " * max(0, width - display_width(clipped))


def _clip_cells(line: str, width: int) -> str:
    if display_width(line) <= width:
        return line
    out: list[str] = []
    used = 0
    for cluster in wrap_cells(line, max(1, width)):
        out.append(cluster)
        used += display_width(cluster)
        if used >= width:
            break
    return "".join(out)[: max(1, width)]


def compute_layout(
    rows: int,
    cols: int,
    *,
    status_height: int,
    boss_height: int,
) -> ScreenLayout:
    """Split the screen so Status and Boss stay fixed and Activity absorbs slack."""

    rows = max(1, rows)
    cols = max(1, cols)
    if rows < MIN_ROWS_FOR_THREE_PANES:
        # Too small for three panes: keep a usable Boss line and the rest activity.
        boss = 1
        status = max(0, rows - boss - MIN_PANE_HEIGHT)
        activity = max(MIN_PANE_HEIGHT, rows - status - boss)
        status = max(0, rows - boss - activity)
        return ScreenLayout(rows, cols, status, activity, boss, ruled=False)
    budget = rows - 2  # two separator rules
    wanting_boss = max(MIN_PANE_HEIGHT, boss_height)
    wanting_status = max(MIN_PANE_HEIGHT, status_height)
    # Activity keeps at least one line for itself.
    boss = min(wanting_boss, max(MIN_PANE_HEIGHT, budget - MIN_PANE_HEIGHT * 2))
    status = min(wanting_status, max(MIN_PANE_HEIGHT, budget - boss - MIN_PANE_HEIGHT))
    activity = max(MIN_PANE_HEIGHT, budget - status - boss)
    # Absorb any rounding slack into activity so status/boss never grow.
    remainder = budget - (status + activity + boss)
    if remainder > 0:
        activity += remainder
    return ScreenLayout(rows, cols, status, activity, boss, ruled=True)


def render_screen(
    layout: ScreenLayout,
    *,
    status: tuple[str, ...],
    activity: tuple[str, ...],
    boss: tuple[str, ...],
) -> tuple[str, ...]:
    """Compose exactly ``layout.rows`` fixed-height lines."""

    width = layout.cols
    status_block = _fit(status, layout.status_height, width, top=True)
    boss_block = _fit(boss, layout.boss_height, width, top=False)
    # Activity keeps the newest line visible at the bottom (tail-anchored).
    activity_block = _fit(activity, layout.activity_height, width, top=False)
    if not layout.ruled:
        lines = status_block + activity_block + boss_block
    else:
        rule = RULE * width
        lines = status_block + (rule,) + activity_block + (rule,) + boss_block
    if len(lines) != layout.rows:  # defensive: geometry must always sum exactly
        lines = tuple(list(lines)[: layout.rows])
        lines = lines + (" " * width,) * (layout.rows - len(lines))
    return lines


def wrapped_height(lines: tuple[str, ...], width: int) -> int:
    """Rows this content needs after cell-aware wrapping at ``width``."""

    return sum(
        max(1, len(wrap_cells(line, width))) for line in lines
    )


__all__ = [
    "MIN_ROWS_FOR_THREE_PANES",
    "Pane",
    "RULE",
    "ScreenLayout",
    "compute_layout",
    "render_screen",
    "wrapped_height",
]
