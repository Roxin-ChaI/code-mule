"""Persistent terminal UI: a presentation/controller layer over the CLI.

The UI never owns business truth.  It reads persisted ProjectState, renders it,
and dispatches Boss input through the same command layer the one-shot CLI uses.
"""

from .activity import ActivityEntry, ActivityKind, ActivityLog
from .layout import Pane, ScreenLayout, compute_layout, render_screen
from .snapshot import TerminalSnapshot, build_snapshot, status_lines

__all__ = [
    "ActivityEntry",
    "ActivityKind",
    "ActivityLog",
    "Pane",
    "ScreenLayout",
    "TerminalSnapshot",
    "build_snapshot",
    "compute_layout",
    "render_screen",
    "status_lines",
]
