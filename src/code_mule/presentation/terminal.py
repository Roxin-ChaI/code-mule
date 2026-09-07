"""Scrollback-friendly, display-cell-aware terminal layout; no runtime I/O."""

from dataclasses import dataclass
import os
import shutil
import unicodedata
from typing import TextIO


def safe_text(value: str) -> str:
    """Render terminal controls visibly rather than executing them."""
    return "".join(
        character if character == "\n" or unicodedata.category(character) not in {"Cc", "Cs"}
        else f"\\x{ord(character):02x}"
        for character in str(value)
    )


def _clusters(value: str) -> list[str]:
    clusters: list[str] = []
    for char in value:
        extend = (unicodedata.combining(char) or char in {"\ufe0e", "\ufe0f", "\u200d"}
                  or 0x1F3FB <= ord(char) <= 0x1F3FF)
        regional = 0x1F1E6 <= ord(char) <= 0x1F1FF
        if clusters and (extend or clusters[-1].endswith("\u200d")
                         or (regional and len(clusters[-1]) == 1 and 0x1F1E6 <= ord(clusters[-1]) <= 0x1F1FF)):
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def _cells(cluster: str) -> int:
    widths = [2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
              for char in cluster if not unicodedata.combining(char)
              and unicodedata.category(char) != "Cf" and char not in {"\ufe0e", "\ufe0f"}]
    if "\u200d" in cluster or "\ufe0f" in cluster or any(0x1F1E6 <= ord(c) <= 0x1F1FF for c in cluster):
        return 2
    return max(widths, default=0)


def display_width(value: str) -> int:
    return sum(_cells(cluster) for cluster in _clusters(value))


def wrap_cells(value: str, width: int) -> tuple[str, ...]:
    """Wrap without dropping long IDs/commands, including CJK and combining text."""
    width = max(2, width)
    result: list[str] = []
    for paragraph in safe_text(value).split("\n"):
        remaining = _clusters(paragraph)
        if not remaining:
            result.append("")
        while remaining:
            used = end = 0
            while end < len(remaining) and used + _cells(remaining[end]) <= width:
                used += _cells(remaining[end])
                end += 1
            if end < len(remaining):
                spaces = [index for index in range(end) if remaining[index] == " "]
                if spaces and spaces[-1] > 0:
                    end = spaces[-1] + 1
            result.append("".join(remaining[:end]).rstrip())
            remaining = remaining[end:]
    return tuple(result)


@dataclass(frozen=True)
class DashboardSection:
    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class TerminalDashboard:
    width: int = 80
    interactive: bool = False
    unicode: bool = True

    @classmethod
    def for_stream(cls, stream: TextIO, *, width: int | None = None) -> "TerminalDashboard":
        interactive = bool(stream.isatty()) and os.environ.get("TERM") != "dumb"
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            "┌─┐│└┘█░".encode(encoding)
            unicode = True
        except (UnicodeEncodeError, LookupError):
            unicode = False
        if width is None:
            try:
                width = os.get_terminal_size(stream.fileno()).columns
            except (OSError, ValueError, AttributeError):
                width = shutil.get_terminal_size((80, 24)).columns
        return cls(width, interactive and unicode and width >= 12, unicode)

    @property
    def content_width(self) -> int:
        return max(2, min(self.width - 2, 132) - 4)

    def progress(self, completed: int, total: int) -> str:
        total = max(0, total)
        completed = min(max(0, completed), total)
        count = f"{completed} / {total}"
        if not self.interactive or self.width < 60:
            return count
        size = max(4, min(32, self.content_width - 16 - len(count)))
        filled = 0 if total == 0 else completed * size // total
        return "█" * filled + "░" * (size - filled) + "  " + count

    def render(self, sections: tuple[DashboardSection, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for section in sections:
            if result:
                result.append("")
            if not self.interactive:
                result.extend((safe_text(section.title), *map(safe_text, section.lines)))
                continue
            width = self.content_width
            result.append("┌" + "─" * (width + 2) + "┐")
            for line in wrap_cells(section.title, width):
                result.append("│ " + line + " " * (width - display_width(line)) + " │")
            result.append("├" + "─" * (width + 2) + "┤")
            for value in section.lines:
                for line in wrap_cells(value, width):
                    result.append("│ " + line + " " * (width - display_width(line)) + " │")
            result.append("└" + "─" * (width + 2) + "┘")
        if not self.unicode:
            return tuple(line.encode("ascii", "backslashreplace").decode("ascii") for line in result)
        return tuple(result)

    def legacy(self, lines: tuple[str, ...]) -> tuple[str, ...]:
        """Group existing presentation headings; never interpret runtime outcomes."""
        if not self.interactive:
            if self.unicode:
                return lines
            return tuple(line.encode("ascii", "backslashreplace").decode("ascii") for line in lines)
        sections: list[DashboardSection] = []
        title, content = "CODE MULE", []
        for line in lines:
            if line and line.isupper() and not line.startswith(" ") and ":" not in line:
                if content:
                    sections.append(DashboardSection(title, tuple(content)))
                title, content = line, []
            elif line and set(line) <= {"─", "-"}:
                continue
            else:
                content.append(line)
        if content or not sections:
            sections.append(DashboardSection(title, tuple(content)))
        return self.render(tuple(sections))


def row(label: str, value: object) -> str:
    return f"{label:<15}{value}"
