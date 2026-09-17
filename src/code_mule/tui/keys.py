"""Resolved, safe key vocabulary for the persistent terminal.

Only these names are ever stored or displayed.  Raw escape payloads and
unresolved byte sequences are never shown, so the demo can report exactly which
key curses delivered without leaking terminal control data.
"""


KEY_NAMES = (
    "KEY_UP",
    "KEY_DOWN",
    "KEY_PPAGE",
    "KEY_NPAGE",
    "KEY_HOME",
    "KEY_END",
    "KEY_RESIZE",
    "KEY_ENTER",
    "ENTER",
    "BACKSPACE",
    "CTRL_L",
    "CTRL_C",
    "TEXT",
    "UNKNOWN",
)


def key_name(key: object) -> str:
    """Resolved name for one curses key, never an escape payload."""

    import curses

    if key == curses.KEY_UP:
        return "KEY_UP"
    if key == curses.KEY_DOWN:
        return "KEY_DOWN"
    if key == curses.KEY_PPAGE:
        return "KEY_PPAGE"
    if key == curses.KEY_NPAGE:
        return "KEY_NPAGE"
    if key == curses.KEY_HOME:
        return "KEY_HOME"
    if key == curses.KEY_END:
        return "KEY_END"
    if key == curses.KEY_RESIZE:
        return "KEY_RESIZE"
    if key == curses.KEY_ENTER:
        return "KEY_ENTER"
    if isinstance(key, str):
        if key == "\x03":
            return "CTRL_C"
        if key == "\x0c":
            return "CTRL_L"
        if key in ("\n", "\r"):
            return "ENTER"
        if key in ("\x7f", "\b"):
            return "BACKSPACE"
        if key.isprintable():
            return "TEXT"
    return "UNKNOWN"


__all__ = ["KEY_NAMES", "key_name"]
