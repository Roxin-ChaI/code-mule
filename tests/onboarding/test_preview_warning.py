"""Deterministic tests for the preview width wrap warning."""

from pathlib import Path
import os
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"
PREVIEW = ROOT / "scripts" / "preview_terminal.py"


class PreviewTerminalWarningTests(unittest.TestCase):
    def run_preview(self, width, columns):
        environment = dict(os.environ)
        environment["COLUMNS"] = str(columns)
        environment.pop("LINES", None)
        completed = subprocess.run(
            (
                str(PYTHON),
                str(PREVIEW),
                "--width",
                str(width),
                "--plain",
            ),
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def test_wide_preview_warns_when_it_exceeds_terminal_width(self):
        output = self.run_preview(width=120, columns=75)
        self.assertIn(
            "Requested preview width exceeds current terminal width.",
            output,
        )
        self.assertIn("Output may wrap.", output)

    def test_preview_within_terminal_width_does_not_warn(self):
        output = self.run_preview(width=80, columns=100)
        self.assertNotIn(
            "Requested preview width exceeds current terminal width.",
            output,
        )


if __name__ == "__main__":
    unittest.main()
