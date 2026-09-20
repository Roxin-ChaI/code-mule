from io import StringIO
from unittest import TestCase
from unittest.mock import patch

from code_mule.presentation.terminal import DashboardSection, TerminalDashboard, display_width, wrap_cells


class TerminalLayoutTests(TestCase):
    def test_responsive_screenshot_regression(self):
        for width in (40, 60, 80, 120):
            with self.subTest(width=width):
                layout = TerminalDashboard(width, True)
                lines = layout.render((DashboardSection("CODE MULE · My Project", ("Status         Ready", "Plan           —", "Progress       " + layout.progress(0, 0), "Current        None")),))
                self.assertEqual(display_width(lines[0]), width - 2)
                self.assertTrue(all(display_width(line) <= width for line in lines))
                self.assertNotIn("ME ACTION", "\n".join(lines))

    def test_cjk_combining_and_emoji_display_cells(self):
        self.assertEqual(display_width("中文e\u0301"), 5)
        self.assertEqual(display_width("👩‍💻"), 2)
        self.assertEqual(display_width("🇨🇳"), 2)
        value = "请决定排行榜存储方案" * 12
        lines = wrap_cells(value, 34)
        self.assertEqual("".join(lines), value)
        self.assertTrue(all(display_width(line) <= 34 for line in lines))

    def test_long_identifier_is_not_truncated(self):
        value = "action-" + "1234567890" * 10
        self.assertEqual("".join(wrap_cells(value, 34)), value)

    def test_control_characters_cannot_inject_terminal_commands(self):
        lines = TerminalDashboard(80, True).render((DashboardSection("TITLE", ("unsafe\x1b[2J",)),))
        self.assertNotIn("\x1b", "\n".join(lines))

    def test_plain_output_and_ascii_fallback(self):
        value = (DashboardSection("PROJECT", ("Status Ready", "中文")),)
        self.assertEqual(TerminalDashboard().render(value), ("PROJECT", "Status Ready", "中文"))
        self.assertTrue("\n".join(TerminalDashboard(unicode=False).render(value)).isascii())

    def test_term_dumb_and_no_color(self):
        class TTY(StringIO):
            def isatty(self): return True
        with patch.dict("os.environ", {"TERM": "dumb", "NO_COLOR": "1"}):
            self.assertFalse(TerminalDashboard.for_stream(TTY()).interactive)
        with patch.dict("os.environ", {"TERM": "xterm", "NO_COLOR": "1"}):
            output = TerminalDashboard.for_stream(TTY()).render((DashboardSection("PROJECT", ("Ready",)),))
            self.assertNotIn("\x1b", "\n".join(output))

    def test_progress_zero_narrow_and_complete(self):
        self.assertEqual(TerminalDashboard(40, True).progress(4, 6), "4 / 6")
        self.assertNotIn("█", TerminalDashboard(80, True).progress(0, 0))
        self.assertNotIn("░", TerminalDashboard(80, True).progress(6, 6))
