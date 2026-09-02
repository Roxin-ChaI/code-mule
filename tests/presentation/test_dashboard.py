from dataclasses import replace
from datetime import UTC, datetime
import unittest

from code_mule.presentation.dashboard import render_dashboard
from code_mule.progress import ProgressEvent, ProgressEventType, ProgressSnapshot


NOW = datetime(2026, 9, 2, tzinfo=UTC)


def snapshot(status="running"):
    return ProgressSnapshot(
        project_id="project-1",
        project_status=status,
        completed_tasks=3,
        total_tasks=5,
        current_task_id="T4",
        current_task_title="Add multiply tests",
        current_attempt=1,
        worker_status="Working",
        supervisor_status="Waiting",
        project_started_at=NOW,
        task_started_at=NOW,
        stage_started_at=NOW,
        recent_events=(
            ProgressEvent(
                ProgressEventType.TASK_DISPATCHED,
                NOW,
                "project-1",
                "T4",
                None,
                "T4 dispatched",
                {},
            ),
        ),
        project_name="Calculator",
        plan_version=2,
        worker_activity="Running tests",
    )


def render(value, *, width=80, ascii_only=False, final=True):
    return render_dashboard(
        value,
        project_elapsed="08:42",
        stage_elapsed="00:42",
        spinner="|",
        final=final,
        width=width,
        ascii_only=ascii_only,
    )


class DashboardPresentationTests(unittest.TestCase):
    def test_running_dashboard_has_all_boss_sections(self):
        output = "\n".join(render(snapshot(), final=False))
        for heading in (
            "PROJECT", "CURRENT TASK", "WORKER", "SUPERVISOR", "RECENT ACTIVITY"
        ):
            self.assertIn(heading, output)
        self.assertIn("Project     Calculator", output)
        self.assertIn("Status      Running", output)
        self.assertIn("Plan        v2", output)
        self.assertIn("3 / 5", output)
        self.assertIn("Activity    Running tests", output)

    def test_control_and_terminal_states_are_human_readable(self):
        expected = {
            "done": "Completed",
            "change_requested": "Change requested",
            "replanning": "Replanning",
            "human_required": "Action required",
        }
        for raw, label in expected.items():
            with self.subTest(raw=raw):
                output = "\n".join(render(replace(snapshot(), project_status=raw)))
                self.assertIn(f"Status      {label}", output)
        human = "\n".join(render(replace(snapshot(), project_status="human_required")))
        self.assertIn("! ACTION REQUIRED", human)
        self.assertIn("No action has been executed.", human)

    def test_worker_and_supervisor_failure_are_visible_without_raw_enum_noise(self):
        failed = replace(
            snapshot(),
            worker_status="FAILED",
            supervisor_status="FAILED",
            recent_events=(
                ProgressEvent(
                    ProgressEventType.SUPERVISOR_FAILED,
                    NOW,
                    "project-1",
                    "T4",
                    1,
                    "Supervisor review failed",
                    {},
                ),
            ),
        )
        output = "\n".join(render(failed))
        self.assertIn("Codex       Failed", output)
        self.assertIn("DeepSeek    Failed", output)
        self.assertIn("! Supervisor review failed", output)

    def test_narrow_terminal_truncates_every_line_safely(self):
        narrow = replace(
            snapshot(),
            current_task_title="A very long task title that cannot fit in a narrow terminal",
            worker_activity="A very long safe activity message from Codex",
        )
        lines = render(narrow, width=24)
        self.assertTrue(all(len(line) <= 24 for line in lines))
        self.assertTrue(any(line.endswith("…") for line in lines))

    def test_ascii_fallback_has_no_unicode_symbols(self):
        output = "\n".join(render(snapshot(), ascii_only=True))
        for symbol in ("✓", "→", "█", "░", "─", "·", "—"):
            self.assertNotIn(symbol, output)
        self.assertIn("> T4 dispatched", output)
        self.assertIn("###", output)


if __name__ == "__main__":
    unittest.main()
