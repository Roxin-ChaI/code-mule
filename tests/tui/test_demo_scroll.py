"""Demo observability: numbered activity, scroll indicator, and safe key names."""

from __future__ import annotations

from datetime import UTC, datetime
import io
import unittest

from code_mule.tui.activity import ActivityKind, ActivityLog
from code_mule.tui.app import fallback_lines
from code_mule.tui.controller import TerminalController
from code_mule.tui.demo import DEMO_ACTIVITY_COUNT, demo_commands, demo_state, seed_demo_activity
from code_mule.tui.keys import KEY_NAMES, key_name
from code_mule.tui.layout import compute_layout, render_screen
from code_mule.tui.snapshot import build_snapshot


NOW = datetime(2026, 9, 18, tzinfo=UTC)


def demo_controller() -> TerminalController:
    control = TerminalController(
        demo_commands(), demo_state, input_stream=io.StringIO("")
    )
    control.state.key_debug = True
    seed_demo_activity(control)
    return control


class DemoActivityTests(unittest.TestCase):
    def test_demo_seeds_at_least_sixty_numbered_events(self):
        control = demo_controller()
        self.assertGreaterEqual(len(control.activity), DEMO_ACTIVITY_COUNT)
        self.assertGreaterEqual(DEMO_ACTIVITY_COUNT, 60)
        first = control.activity.entries[0].text
        last_event = control.activity.entries[DEMO_ACTIVITY_COUNT - 1].text
        self.assertTrue(first.startswith("event 001"))
        self.assertTrue(last_event.startswith("event 060"))
        self.assertGreaterEqual(DEMO_ACTIVITY_COUNT, 45)
        for index in range(1, DEMO_ACTIVITY_COUNT + 1):
            self.assertIn(f"event {index:03d}", control.activity.entries[index - 1].text)

    def test_sixty_events_overflow_an_80x45_activity_region(self):
        control = demo_controller()
        layout = compute_layout(45, 80, status_height=8, boss_height=4)
        self.assertLess(layout.activity_height, DEMO_ACTIVITY_COUNT)
        window = control.activity.visible(layout.activity_height)
        self.assertEqual(len(window), layout.activity_height)
        self.assertTrue(any("event 060" in entry.text for entry in window))


class ScrollIndicatorTests(unittest.TestCase):
    def test_indicator_reports_the_visible_window_and_follow_state(self):
        log = ActivityLog()
        for index in range(1, 61):
            log.append(NOW, ActivityKind.INFO, f"event {index:03d}")
        self.assertEqual(log.window_label(20), "Activity 41-60 / 60 · Follow ON")
        log.scroll(20, visible=20)
        self.assertEqual(log.window_label(20), "Activity 21-40 / 60 · Follow OFF")
        log.scroll_to_latest()
        self.assertEqual(log.window_label(20), "Activity 41-60 / 60 · Follow ON")

    def test_indicator_stays_pinned_to_the_top_of_the_activity_region(self):
        control = demo_controller()
        layout = compute_layout(45, 80, status_height=8, boss_height=4)
        window_height = layout.activity_height - 1
        entries = control.activity.visible(window_height)
        snapshot = build_snapshot(
            demo_state(),
            tuple(entry.text for entry in entries),
            indicator=control.activity.window_label(window_height),
            now=NOW,
        )
        lines = render_screen(
            layout,
            status=snapshot.status,
            activity=snapshot.activity,
            boss=snapshot.boss,
            indicator=snapshot.indicator,
        )
        indicator_row = layout.status_height + 1
        self.assertTrue(lines[indicator_row].startswith("Activity "))
        self.assertIn("Follow ON", lines[indicator_row])
        # The indicator never consumes the Boss input row.
        self.assertTrue(lines[-1].startswith("me>"))

    def test_indicator_tracks_scrolling_and_end_in_a_rendered_frame(self):
        control = demo_controller()
        layout = compute_layout(45, 80, status_height=8, boss_height=4)
        window_height = layout.activity_height - 1

        def frame() -> tuple[str, ...]:
            entries = control.activity.visible(window_height)
            snapshot = build_snapshot(
                demo_state(),
                tuple(entry.text for entry in entries),
                indicator=control.activity.window_label(window_height),
                now=NOW,
            )
            return render_screen(
                layout,
                status=snapshot.status,
                activity=snapshot.activity,
                boss=snapshot.boss,
                indicator=snapshot.indicator,
            )

        self.assertIn("Follow ON", frame()[layout.status_height + 1])
        control.scroll(20, visible=window_height)
        self.assertIn("Follow OFF", frame()[layout.status_height + 1])
        control.scroll_to_latest()
        self.assertIn("Follow ON", frame()[layout.status_height + 1])
        self.assertIn("event 060", "\n".join(frame()))

    def test_window_shrinks_by_one_row_when_the_indicator_is_drawn(self):
        control = demo_controller()
        layout = compute_layout(45, 80, status_height=8, boss_height=4)
        without = render_screen(
            layout,
            status=("s",),
            activity=tuple(entry.text for entry in control.activity.visible(layout.activity_height)),
            boss=("me> ",),
        )
        with_indicator = render_screen(
            layout,
            status=("s",),
            activity=tuple(
                entry.text
                for entry in control.activity.visible(layout.activity_height - 1)
            ),
            boss=("me> ",),
            indicator="Activity 41-60 / 60 · Follow ON",
        )
        self.assertEqual(len(without), len(with_indicator))


class KeyObservabilityTests(unittest.TestCase):
    def test_known_keys_map_to_stable_safe_names(self):
        import curses

        cases = {
            curses.KEY_UP: "KEY_UP",
            curses.KEY_DOWN: "KEY_DOWN",
            curses.KEY_PPAGE: "KEY_PPAGE",
            curses.KEY_NPAGE: "KEY_NPAGE",
            curses.KEY_END: "KEY_END",
            curses.KEY_HOME: "KEY_HOME",
            curses.KEY_RESIZE: "KEY_RESIZE",
            "\x03": "CTRL_C",
            "\x0c": "CTRL_L",
            "\n": "ENTER",
            "\x7f": "BACKSPACE",
            "a": "TEXT",
            "\u4e2d": "TEXT",
        }
        for key, expected in cases.items():
            with self.subTest(key=expected):
                self.assertEqual(key_name(key), expected)

    def test_raw_escape_payloads_are_never_reported_as_a_key_name(self):
        for payload in ("\x1b[6~", "\x1b[5~", "\x1bOF", "\x1b[1;2C", "\x1b"):
            with self.subTest(payload=repr(payload)):
                name = key_name(payload)
                self.assertEqual(name, "UNKNOWN")
                self.assertIn(name, KEY_NAMES)
                self.assertNotIn("\x1b", name)
                self.assertNotIn("[", name)

    def test_key_debug_display_is_opt_in_and_name_only(self):
        import curses

        control = TerminalController(
            demo_commands(), demo_state, input_stream=io.StringIO("")
        )
        control.note_key("KEY_PPAGE")
        self.assertIsNone(control.key_debug, "hidden unless debug is enabled")
        control.state.key_debug = True
        self.assertEqual(control.key_debug, "KEY_PPAGE")
        control.note_key("\x1b[6~")
        self.assertEqual(control.key_debug, "UNKNOWN")
        control.note_key(key_name(curses.KEY_NPAGE))
        self.assertEqual(control.key_debug, "KEY_NPAGE")

    def test_initial_readout_is_an_em_dash_at_the_activity_status_line(self):
        control = demo_controller()
        self.assertEqual(control.key_display, "—")
        lines = fallback_lines(control, rows=45, cols=80)
        status_row = next(line for line in lines if line.startswith("Activity "))
        self.assertIn("Key: —", status_row)
        # The readout must never sit on, or displace, the Boss input row.
        self.assertEqual(lines[-1].rstrip(), "me>")

    def test_readout_updates_through_the_real_key_handler(self):
        import curses

        from code_mule.tui.app import _handle_key

        control = demo_controller()
        seen = []
        for key, expected in (
            (curses.KEY_UP, "KEY_UP"),
            (curses.KEY_DOWN, "KEY_DOWN"),
            (curses.KEY_PPAGE, "KEY_PPAGE"),
            (curses.KEY_NPAGE, "KEY_NPAGE"),
            (curses.KEY_END, "KEY_END"),
            ("\x0c", "CTRL_L"),
            ("\x1b[6~", "UNKNOWN"),
        ):
            _handle_key(control, key, rows=45, cols=80)
            row = next(
                line
                for line in fallback_lines(control, rows=45, cols=80)
                if line.startswith("Activity ")
            )
            self.assertIn(f"Key: {expected}", row)
            seen.append(expected)
        self.assertEqual(seen[-1], "UNKNOWN")
        self.assertNotIn("\x1b", "".join(fallback_lines(control, rows=45, cols=80)))

    def test_readout_survives_resize_and_stays_off_the_boss_row(self):
        import curses

        from code_mule.tui.app import _handle_key

        control = demo_controller()
        _handle_key(control, curses.KEY_PPAGE, rows=45, cols=80)
        for rows, cols in ((45, 80), (24, 80), (40, 120)):
            with self.subTest(rows=rows, cols=cols):
                lines = fallback_lines(control, rows=rows, cols=cols)
                self.assertEqual(len(lines), rows)
                self.assertIn("Key: KEY_PPAGE", "\n".join(lines))
                self.assertEqual(lines[-1].rstrip(), "me>")
        _handle_key(control, curses.KEY_RESIZE, rows=120, cols=200)
        lines = fallback_lines(control, rows=40, cols=120)
        self.assertIn("Key: KEY_RESIZE", "\n".join(lines))
        self.assertEqual(lines[-1].rstrip(), "me>")

    def test_key_name_is_recorded_by_the_key_handler(self):
        import curses

        from code_mule.tui.app import _handle_key

        control = demo_controller()
        _handle_key(control, curses.KEY_PPAGE, rows=45, cols=80)
        self.assertEqual(control.key_debug, "KEY_PPAGE")
        _handle_key(control, curses.KEY_END, rows=45, cols=80)
        self.assertEqual(control.key_debug, "KEY_END")


class DemoFrameTests(unittest.TestCase):
    def test_script_and_cli_demo_entry_points_both_enable_key_debug(self):
        import importlib.util
        from pathlib import Path
        import sys

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "tui_demo_script", root / "scripts" / "tui_demo.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules["tui_demo_script"] = module
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, "main"))

        from code_mule.cli.composition import ProductionCliComposition

        composition = ProductionCliComposition(
            Path("/tmp/nonexistent-state.json"),
            environment={},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            stdin=io.StringIO(),
        )
        result = composition.ui(demo=True)
        text = "\n".join(result.output)
        # Both demo entries must show the readout, starting at the em dash.
        self.assertIn("Key: —", text)
        self.assertIn("Activity ", text)

    def test_script_demo_renders_the_readout_before_any_key(self):
        control = demo_controller()
        text = "\n".join(fallback_lines(control, rows=45, cols=80))
        self.assertIn("Key: —", text)
        self.assertIn("Activity ", text)

    def test_demo_frame_is_bounded_for_the_documented_sizes(self):
        control = demo_controller()
        for rows, cols in ((45, 80), (24, 80), (40, 120)):
            with self.subTest(rows=rows, cols=cols):
                lines = fallback_lines(control, rows=rows, cols=cols)
                self.assertEqual(len(lines), rows)
                self.assertTrue(lines[-1].startswith("me>"))
                self.assertIn("Activity ", "\n".join(lines))

    def test_non_tty_demo_output_still_reports_the_fallback(self):
        from code_mule.tui.app import run_terminal

        control = demo_controller()
        out = io.StringIO()
        self.assertEqual(run_terminal(control, stdin=io.StringIO(), stdout=out), 0)
        text = out.getvalue()
        self.assertIn("persistent UI unavailable", text)
        self.assertIn("Activity ", text)


if __name__ == "__main__":
    unittest.main()
