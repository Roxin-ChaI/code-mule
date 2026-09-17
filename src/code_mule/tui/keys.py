"""Resolved, safe key vocabulary for the persistent terminal.

Only these names are ever stored or displayed.  Raw escape payloads and
unresolved byte sequences are never shown, so the demo can report exactly which
key curses delivered without leaking terminal control data.
"""

from dataclasses import dataclass


KEY_NAMES = (
    "KEY_UP",
    "KEY_DOWN",
    "KEY_LEFT",
    "KEY_RIGHT",
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

ESCAPE = "\x1b"
MAX_ESCAPE_LENGTH = 8

# Terminfo capabilities are the primary source: they are the terminal's own
# declaration, not a guess.  macOS Terminal reports TERM=xterm-256color, whose
# `kend` is ESC O F — which is exactly the Fn+Right sequence.
TERMINFO_CAPABILITIES = (
    ("kcuu1", "KEY_UP"),
    ("kcud1", "KEY_DOWN"),
    ("kcub1", "KEY_LEFT"),
    ("kcuf1", "KEY_RIGHT"),
    ("khome", "KEY_HOME"),
    ("kend", "KEY_END"),
    ("kpp", "KEY_PPAGE"),
    ("knp", "KEY_NPAGE"),
)

# Documented ECMA-48/xterm sequences, used only when terminfo has no capability
# for this TERM (for example TERM=dumb or a minimal terminfo database).  These
# are the published xterm-family encodings, not arbitrary byte guessing.
FALLBACK_SEQUENCES: tuple[tuple[str, str], ...] = (
    ("\x1b[A", "KEY_UP"),
    ("\x1b[B", "KEY_DOWN"),
    ("\x1b[D", "KEY_LEFT"),
    ("\x1b[C", "KEY_RIGHT"),
    ("\x1bOA", "KEY_UP"),
    ("\x1bOB", "KEY_DOWN"),
    ("\x1bOD", "KEY_LEFT"),
    ("\x1bOC", "KEY_RIGHT"),
    ("\x1b[H", "KEY_HOME"),
    ("\x1bOH", "KEY_HOME"),
    ("\x1b[F", "KEY_END"),
    ("\x1bOF", "KEY_END"),
    ("\x1b[4~", "KEY_END"),
    ("\x1b[1~", "KEY_HOME"),
    ("\x1b[5~", "KEY_PPAGE"),
    ("\x1b[6~", "KEY_NPAGE"),
)


@dataclass(frozen=True)
class SequenceTable:
    """Sequence -> key name, built from terminfo with a documented fallback."""

    mapping: dict[str, str]
    source: str

    def resolve(self, sequence: str) -> str | None:
        return self.mapping.get(sequence)

    def is_prefix(self, sequence: str) -> bool:
        """Whether more input could still complete a known sequence."""

        return any(
            candidate.startswith(sequence) and candidate != sequence
            for candidate in self.mapping
        )


def sequence_table(*, setupterm=None) -> SequenceTable:
    """Build the sequence table, preferring the terminal's own terminfo."""

    mapping: dict[str, str] = {}
    source = "fallback"
    if setupterm is not False:
        try:
            import curses

            if setupterm is not None:
                setupterm()
            else:
                curses.setupterm()
            found = False
            for capability, name in TERMINFO_CAPABILITIES:
                raw = curses.tigetstr(capability)
                if not raw:
                    continue
                try:
                    decoded = raw.decode("latin-1")
                except Exception:  # pragma: no cover - defensive
                    continue
                if decoded.startswith(ESCAPE):
                    mapping[decoded] = name
                    found = True
            if found:
                source = "terminfo"
        except Exception:
            pass
    for sequence, name in FALLBACK_SEQUENCES:
        mapping.setdefault(sequence, name)
    if source == "terminfo":
        # A terminal that declares its own End must win over the fallback.
        for sequence, name in FALLBACK_SEQUENCES:
            mapping.setdefault(sequence, name)
    return SequenceTable(mapping, source)


def resolve_sequence(sequence: str, table: SequenceTable | None = None) -> str:
    """Resolve one complete escape sequence to a safe key name."""

    if not isinstance(sequence, str) or not sequence.startswith(ESCAPE):
        return "UNKNOWN"
    resolved = (table or sequence_table()).resolve(sequence)
    return resolved if resolved in KEY_NAMES else "UNKNOWN"


def key_name(key: object) -> str:
    """Resolved name for one curses key, never an escape payload."""

    import curses

    if key == curses.KEY_UP:
        return "KEY_UP"
    if key == curses.KEY_DOWN:
        return "KEY_DOWN"
    if key == curses.KEY_LEFT:
        return "KEY_LEFT"
    if key == curses.KEY_RIGHT:
        return "KEY_RIGHT"
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


__all__ = [
    "ESCAPE",
    "FALLBACK_SEQUENCES",
    "KEY_NAMES",
    "MAX_ESCAPE_LENGTH",
    "SequenceTable",
    "TERMINFO_CAPABILITIES",
    "key_name",
    "resolve_sequence",
    "sequence_table",
]
