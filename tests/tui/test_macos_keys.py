"""macOS Terminal key decoding: terminfo-first, bounded ESC aggregation."""

from __future__ import annotations

import io
import unittest

from code_mule.tui.activity import ActivityKind
from code_mule.tui.app import _handle_key, read_key_event
from code_mule.tui.controller import TerminalController
from code_mule.tui.demo import demo_commands, demo_state, seed_demo_activity
from code_mule.tui.keys import (
    ESCAPE,
    FALLBACK_SEQUENCES,
    KEY_NAMES,
    MAX_ESCAPE_LENGTH,
    InputKind,
    SequenceTable,
    TERMINFO_CAPABILITIES,
    key_name,
    resolve_sequence,
    sequence_table,
)


class FakeScreen:
    """Feeds scripted keys, one ``get_wch`` call at a time."""

    def __init__(self, keys) -> None:
        self.keys = list(keys)
        self.timeouts: list[int] = []

    def get_wch(self):
        return self.keys.pop(0) if self.keys else None

    def timeout(self, milliseconds) -> None:
        self.timeouts.append(milliseconds)


def xterm_table() -> SequenceTable:
    """The real xterm-256color capabilities macOS Terminal reports."""

    mapping = {
        "\x1bOA": "KEY_UP",
        "\x1bOB": "KEY_DOWN",
        "\x1bOD": "KEY_LEFT",
        "\x1bOC": "KEY_RIGHT",
        "\x1bOH": "KEY_HOME",
        "\x1bOF": "KEY_END",
        "\x1b[5~": "KEY_PPAGE",
        "\x1b[6~": "KEY_NPAGE",
    }
    # sequence_table() merges the documented fallbacks under terminfo, so the
    # fixture mirrors that merged table.
    for sequence, name in FALLBACK_SEQUENCES:
        mapping.setdefault(sequence, name)
    return SequenceTable(mapping, "terminfo")


def controller() -> TerminalController:
    control = TerminalController(
        demo_commands(), demo_state, input_stream=io.StringIO("")
    )
    control.state.key_debug = True
    seed_demo_activity(control)
    return control


class TerminfoTableTests(unittest.TestCase):
    def test_terminfo_capabilities_cover_every_documented_key(self):
        names = {name for _capability, name in TERMINFO_CAPABILITIES}
        self.assertEqual(
            names,
            {
                "KEY_UP",
                "KEY_DOWN",
                "KEY_LEFT",
                "KEY_RIGHT",
                "KEY_HOME",
                "KEY_END",
                "KEY_PPAGE",
                "KEY_NPAGE",
            },
        )
        self.assertTrue(names <= set(KEY_NAMES))

    def test_kend_is_the_macos_fn_right_sequence(self):
        table = xterm_table()
        # xterm-256color: kend = ESC O F, which is what Fn+Right sends.
        self.assertEqual(table.resolve("\x1bOF"), "KEY_END")
        self.assertEqual(resolve_sequence("\x1bOF", table), "KEY_END")

    def test_fallback_table_is_used_when_terminfo_is_unavailable(self):
        table = sequence_table(setupterm=False)
        self.assertEqual(table.source, "fallback")
        for sequence, name in FALLBACK_SEQUENCES:
            with self.subTest(sequence=name):
                self.assertEqual(table.resolve(sequence), name)
        self.assertEqual(resolve_sequence("\x1b[F", table), "KEY_END")

    def test_terminal_declared_sequence_wins_over_the_fallback(self):
        table = SequenceTable({"CUSTOM-END": "KEY_END"}, "terminfo")
        self.assertEqual(table.resolve("CUSTOM-END"), "KEY_END")
        self.assertIsNone(table.resolve("\x1b[F"))


class EscapeAggregationTests(unittest.TestCase):
    def test_split_terminfo_end_sequence_resolves_to_key_end(self):
        for split in (list("\x1bOF"), list("\x1b[F"), ["\x1bO", "F"]):
            with self.subTest(split=split):
                screen = FakeScreen(split)
                event = read_key_event(screen, table=xterm_table())
                self.assertIsNotNone(event)
                self.assertEqual(event.key_name, "KEY_END")

    def test_split_right_arrow_stays_key_right_and_never_end(self):
        for split in (list("\x1bOC"), list("\x1b[C")):
            with self.subTest(split=split):
                screen = FakeScreen(split)
                event = read_key_event(screen, table=xterm_table())
                self.assertIsNotNone(event)
                self.assertEqual(event.key_name, "KEY_RIGHT")
                self.assertNotEqual(event.key_name, "KEY_END")

    def test_other_macos_shortcuts_are_unaffected(self):
        cases = {
            "\x1bOA": "KEY_UP",
            "\x1bOB": "KEY_DOWN",
            "\x1bOD": "KEY_LEFT",
            "\x1b[5~": "KEY_PPAGE",
            "\x1b[6~": "KEY_NPAGE",
            "\x1bOH": "KEY_HOME",
        }
        for sequence, expected in cases.items():
            with self.subTest(key=expected):
                screen = FakeScreen(list(sequence))
                event = read_key_event(screen, table=xterm_table())
                self.assertIsNotNone(event)
                self.assertEqual(event.key_name, expected)

    def test_unknown_sequences_resolve_to_unknown(self):
        for sequence in ("\x1b[9~", "\x1b[1;2C", "\x1b", "\x1bZZZZZZZZZZ"):
            with self.subTest(sequence=repr(sequence)):
                screen = FakeScreen(list(sequence))
                event = read_key_event(screen, table=xterm_table())
                self.assertIsNotNone(event)
                self.assertEqual(event.key_name, "UNKNOWN")
                self.assertIn(event.key_name, KEY_NAMES)
                self.assertNotIn("\x1b", event.key_name)

    def test_aggregation_is_bounded(self):
        screen = FakeScreen([ESCAPE] + ["x"] * 40)
        event = read_key_event(screen, table=xterm_table())
        self.assertIsNotNone(event)
        self.assertEqual(event.key_name, "UNKNOWN")
        consumed = 1 + (40 - len(screen.keys))
        self.assertLessEqual(consumed, 1 + MAX_ESCAPE_LENGTH)

    def test_bare_escape_keypress_is_not_a_hang_and_restores_the_refresh_timeout(self):
        screen = FakeScreen([ESCAPE])
        event = read_key_event(screen, table=xterm_table())
        self.assertIsNotNone(event)
        self.assertEqual(event.key_name, "UNKNOWN")
        self.assertEqual(screen.timeouts[0], 30)
        self.assertEqual(screen.timeouts[-1], 250)

    def test_plain_keys_still_resolve_without_aggregation(self):
        import curses

        cases = {
            curses.KEY_UP: "KEY_UP",
            curses.KEY_PPAGE: "KEY_PPAGE",
            "\x03": "CTRL_C",
            "\x0c": "CTRL_L",
            "\n": "ENTER",
            "a": "TEXT",
        }
        for key, expected in cases.items():
            with self.subTest(key=expected):
                event = read_key_event(FakeScreen([key]), table=xterm_table())
                self.assertIsNotNone(event)
                self.assertEqual(event.key_name, expected)
                if expected == "TEXT":
                    self.assertIs(event.kind, InputKind.TEXT)
                    self.assertEqual(event.text, key)
                else:
                    self.assertIs(event.kind, InputKind.CONTROL)
                    self.assertIsNone(event.text)


class EndBehaviourTests(unittest.TestCase):
    def test_end_returns_the_activity_view_to_follow_on(self):
        control = controller()
        control.scroll(20, visible=10)
        self.assertFalse(control.activity.following)
        _handle_key(control, body_end := read_key_event(
            FakeScreen(list("\x1bOF")), table=xterm_table()
        ), rows=45, cols=80)
        self.assertIsNotNone(body_end)
        self.assertEqual(body_end.key_name, "KEY_END")
        self.assertTrue(control.activity.following)
        self.assertEqual(control.key_display, "KEY_END")
        entries = control.activity.visible(10)
        self.assertTrue(any("event 060" in entry.text for entry in entries))

    def test_end_through_the_scripted_event_loop_marks_follow_on(self):
        import curses

        control = controller()
        control.scroll(15, visible=8)
        self.assertFalse(control.activity.following)
        _handle_key(control, key_name(curses.KEY_END), rows=45, cols=80)
        self.assertTrue(control.activity.following)
        self.assertEqual(control.key_display, "KEY_END")

    def test_right_arrow_does_not_change_scroll_position(self):
        control = controller()
        control.scroll(12, visible=8)
        offset = control.activity.offset
        _handle_key(control, "KEY_RIGHT", rows=45, cols=80)
        self.assertEqual(control.activity.offset, offset)
        self.assertFalse(control.activity.following)
        self.assertEqual(control.key_display, "KEY_RIGHT")

    def test_readout_never_contains_escape_bytes_after_decoding(self):
        control = controller()
        for sequence in ("\x1bOF", "\x1b[9~", "\x1b"):
            name = read_key_event(
                FakeScreen(list(sequence)), table=xterm_table()
            )
            _handle_key(control, name, rows=45, cols=80)
            self.assertNotIn(ESCAPE, control.key_display)
            self.assertIn(control.key_display, KEY_NAMES)

    def test_activity_entries_never_carry_raw_escape_bytes(self):
        control = controller()
        control.activity.append(
            __import__("datetime").datetime.now(__import__("datetime").UTC),
            ActivityKind.INFO,
            "line with \x1b[6~ payload",
        )
        self.assertNotIn("\x1b", control.activity.entries[-1].text)


if __name__ == "__main__":
    unittest.main()
