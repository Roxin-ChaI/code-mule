"""Fixed three-pane geometry, scrolling, and cell-accurate Unicode handling."""

from __future__ import annotations

from datetime import UTC, datetime
import unittest

from code_mule.presentation.terminal import display_width
from code_mule.tui.activity import ActivityKind, ActivityLog
from code_mule.tui.layout import Pane, compute_layout, render_screen
from code_mule.tui.snapshot import build_snapshot, status_lines

from state import make_project_state


NOW = datetime(2026, 9, 18, tzinfo=UTC)
SIZES = ((24, 80), (40, 120), (10, 40), (7, 20), (5, 12))


def frame(rows: int, cols: int, *, activity=(), buffer="", state=None):
    layout = compute_layout(rows, cols, status_height=8, boss_height=4)
    snapshot = build_snapshot(state, tuple(activity), buffer=buffer, now=NOW)
    lines = render_screen(
        layout, status=snapshot.status, activity=snapshot.activity, boss=snapshot.boss
    )
    return layout, lines


class LayoutGeometryTests(unittest.TestCase):
    def _frame(self, rows, cols, *, activity=(), state=None):
        layout = compute_layout(rows, cols, status_height=8, boss_height=4)
        snapshot = build_snapshot(state, tuple(activity), now=NOW)
        lines = render_screen(
            layout,
            status=snapshot.status,
            activity=snapshot.activity,
            boss=snapshot.boss,
        )
        return layout, lines

    def test_activity_is_top_aligned_with_blank_space_below(self):
        for rows, cols in ((24, 80), (45, 80), (40, 120)):
            with self.subTest(rows=rows, cols=cols):
                layout, lines = self._frame(
                    rows, cols,
                    activity=["first activity", "second activity"],
                    state=make_project_state(),
                )
                top_rule = layout.status_height
                first = top_rule + 1
                self.assertEqual(lines[top_rule][0], "─")
                self.assertIn("first activity", lines[first])
                self.assertIn("second activity", lines[first + 1])
                # Blank space stays at the bottom of the activity region.
                for offset in range(2, layout.activity_height):
                    self.assertEqual(lines[first + offset].strip(), "")

    def test_activity_overflow_keeps_the_newest_lines_visible(self):
        layout, lines = self._frame(
            24, 80,
            activity=[f"line {index}" for index in range(40)],
            state=make_project_state(),
        )
        window = lines[layout.status_height + 1: layout.status_height + 1 + layout.activity_height]
        self.assertIn("line 39", "\n".join(window))
        self.assertNotIn("line 0\n", "\n".join(window) + "\n")
        self.assertEqual(window[-1].strip(), "line 39")

    def test_activity_window_shrinks_for_small_terminals(self):
        for rows, cols in ((12, 40), (10, 30)):
            with self.subTest(rows=rows, cols=cols):
                layout, lines = self._frame(
                    rows, cols, activity=["only"], state=make_project_state()
                )
                self.assertGreaterEqual(layout.activity_height, 1)
                self.assertEqual(len(lines), rows)

    def test_every_size_produces_exactly_rows_lines_within_cols_cells(self):
        for rows, cols in SIZES:
            with self.subTest(rows=rows, cols=cols):
                layout, lines = frame(rows, cols, state=make_project_state())
                self.assertEqual(len(lines), rows)
                self.assertEqual(layout.total, rows)
                for line in lines:
                    self.assertLessEqual(display_width(line), cols)

    def test_80x24_and_120x40_keep_a_fixed_header_and_footer(self):
        for rows, cols in ((24, 80), (40, 120)):
            with self.subTest(rows=rows, cols=cols):
                layout, lines = frame(
                    rows, cols, activity=["activity one", "activity two"],
                    state=make_project_state(),
                )
                self.assertTrue(lines[0].startswith("CODE MULE"))
                self.assertIn("Project", lines[1])
                self.assertTrue(lines[-1].startswith("boss>"))
                self.assertEqual(layout.status_height, 8)
                self.assertGreaterEqual(layout.activity_height, 1)
                self.assertEqual(
                    layout.pane_for(0), Pane.STATUS
                )
                self.assertEqual(layout.pane_for(rows - 1), Pane.BOSS)
                self.assertEqual(
                    layout.pane_for(layout.status_height + 1), Pane.ACTIVITY
                )

    def test_resize_recomputes_without_leftover_rows(self):
        for rows, cols in ((24, 80), (40, 120), (18, 60)):
            with self.subTest(rows=rows, cols=cols):
                layout, lines = frame(rows, cols, state=make_project_state())
                self.assertEqual(len(lines), rows)
                self.assertLessEqual(max(display_width(line) for line in lines), cols)
                self.assertTrue(lines[-1].startswith("boss>"))

    def test_status_pane_content_is_derived_from_state(self):
        state = make_project_state()
        lines = status_lines(state)
        text = "\n".join(lines)
        self.assertIn("Code Mule", text)
        self.assertIn("Progress", text)
        self.assertIn("Current", text)
        self.assertIn("Safe Point", text)

    def test_runtime_line_appears_only_when_a_session_exists(self):
        from dataclasses import replace
        from code_mule.runtime_handoff import (
            RuntimeHealthStatus,
            RuntimeSession,
            RuntimeSessionStatus,
        )

        state = make_project_state()
        self.assertNotIn("Runtime", "\n".join(status_lines(state)))
        session = RuntimeSession(
            id="runtime-1", project_id=state.project.id, revision_number=1,
            manifest_id="manifest-r1-1", pid=4242, started_at=NOW,
            status=RuntimeSessionStatus.RUNNING,
            access_url="http://127.0.0.1:49441/",
            health_status=RuntimeHealthStatus.HEALTHY,
            process_start_identity=None, command_fingerprint=None,
        )
        text = "\n".join(status_lines(replace(state, runtime_sessions=(session,))))
        self.assertIn("Runtime", text)
        self.assertIn("49441", text)

    def test_wide_and_combining_characters_stay_within_the_pane(self):
        rows, cols = 24, 80
        activity = ("中文活动日志：worker started", "emoji 👩‍💻 done", "mixed 中文 and ascii text")
        layout, lines = frame(rows, cols, activity=activity, state=make_project_state())
        for line in lines:
            self.assertLessEqual(display_width(line), cols)
        self.assertTrue(any("中文" in line for line in lines))

    def test_frame_without_state_reports_uninitialized_project(self):
        _layout, lines = frame(24, 80)
        self.assertIn("Not initialized", "\n".join(lines))


class ActivityLogTests(unittest.TestCase):
    def _log(self, count: int) -> ActivityLog:
        log = ActivityLog()
        for index in range(count):
            log.append(NOW, ActivityKind.INFO, f"line {index}")
        return log

    def test_default_window_follows_the_newest_entries(self):
        log = self._log(30)
        self.assertTrue(log.following)
        window = log.visible(5)
        self.assertEqual([entry.text for entry in window], [f"line {i}" for i in range(25, 30)])

    def test_scrolling_back_keeps_the_window_bounded_and_reversible(self):
        log = self._log(30)
        log.scroll(10, visible=5)
        self.assertFalse(log.following)
        window = log.visible(5)
        self.assertEqual([entry.text for entry in window], [f"line {15 + i}" for i in range(5)])
        log.scroll(-4, visible=5)
        self.assertEqual(log.offset, 6)
        log.scroll_to_latest()
        self.assertTrue(log.following)

    def test_scroll_never_exceeds_the_available_history(self):
        log = self._log(3)
        log.scroll(99, visible=5)
        self.assertEqual(log.offset, 0)
        log.scroll(-99, visible=5)
        self.assertEqual(log.offset, 0)

    def test_new_entries_keep_following_when_the_boss_has_not_scrolled(self):
        log = self._log(4)
        log.scroll(-2, visible=2)
        self.assertEqual(log.offset, 0)
        log.append(NOW, ActivityKind.TASK, "newest")
        self.assertEqual(log.visible(1)[-1].text, "newest")

    def test_history_is_bounded(self):
        log = ActivityLog(limit=10)
        for index in range(50):
            log.append(NOW, ActivityKind.INFO, f"line {index}")
        self.assertEqual(len(log), 10)
        self.assertEqual(log.visible(10)[0].text, "line 40")

    def test_duplicate_keys_are_ignored(self):
        log = ActivityLog()
        self.assertTrue(log.append(NOW, ActivityKind.INFO, "once", key="k"))
        self.assertFalse(log.append(NOW, ActivityKind.INFO, "twice", key="k"))
        self.assertEqual(len(log), 1)


if __name__ == "__main__":
    unittest.main()
