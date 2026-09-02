from datetime import UTC, datetime, timedelta
from io import StringIO
from time import sleep
import unittest

from code_mule.progress import (
    ConsoleProgressRenderer,
    ProgressEvent,
    ProgressEventType,
)


NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def event(event_type, *, second=0, task_id=None, attempt=None, message=None, metadata=None):
    return ProgressEvent(
        event_type,
        NOW + timedelta(seconds=second),
        "project-1",
        task_id,
        attempt,
        message,
        metadata or {},
    )


class FakeTTY(StringIO):
    def isatty(self):
        return True


class ConsoleProgressRendererTests(unittest.TestCase):
    def test_non_tty_is_line_oriented_ordered_and_has_no_ansi(self):
        stream = StringIO()
        with ConsoleProgressRenderer(stream) as renderer:
            renderer.emit(
                event(
                    ProgressEventType.PROJECT_STARTED,
                    message="Project started",
                    metadata={
                        "project_status": "running",
                        "completed_tasks": "0",
                        "total_tasks": "2",
                    },
                )
            )
            renderer.emit(
                event(
                    ProgressEventType.TASK_DISPATCHED,
                    second=1,
                    task_id="task-1",
                    message="Task dispatched",
                    metadata={"task_title": "First task"},
                )
            )
            renderer.emit(
                event(
                    ProgressEventType.WORKER_STARTED,
                    second=2,
                    task_id="task-1",
                    message="Codex started",
                )
            )
        output = stream.getvalue()
        self.assertNotIn("\x1b", output)
        self.assertLess(output.index("project.started"), output.index("task.dispatched"))
        self.assertLess(output.index("task.dispatched"), output.index("worker.started"))
        self.assertIn("task-1", output)

    def test_snapshot_progress_rework_and_recent_activity_are_deterministic(self):
        renderer = ConsoleProgressRenderer(StringIO())
        renderer.emit(
            event(
                ProgressEventType.PROJECT_STARTED,
                metadata={
                    "project_status": "running",
                    "completed_tasks": "1",
                    "total_tasks": "4",
                },
            )
        )
        for index in range(1, 7):
            renderer.emit(
                event(
                    ProgressEventType.TASK_REWORK,
                    second=index,
                    task_id="task-2",
                    attempt=index,
                    message=f"Rework {index}",
                )
            )
        snapshot = renderer.snapshot
        self.assertEqual(snapshot.percentage, 25.0)
        self.assertEqual(len(snapshot.recent_events), 5)
        self.assertEqual(
            tuple(item.message for item in snapshot.recent_events),
            ("Rework 2", "Rework 3", "Rework 4", "Rework 5", "Rework 6"),
        )

    def test_tty_redraws_and_context_restores_cursor_and_thread(self):
        stream = FakeTTY()
        renderer = ConsoleProgressRenderer(
            stream,
            refresh_interval=0.05,
            monotonic=lambda: 10.0,
        )
        with renderer:
            renderer.emit(
                event(
                    ProgressEventType.PROJECT_STARTED,
                    message="Project started",
                    metadata={
                        "project_status": "running",
                        "completed_tasks": "0",
                        "total_tasks": "1",
                    },
                )
            )
            renderer.emit(
                event(
                    ProgressEventType.WORKER_STARTED,
                    task_id="task-1",
                    message="Codex started",
                )
            )
            self.assertTrue(renderer.thread_alive)
        output = stream.getvalue()
        self.assertIn("\x1b[?25l", output)
        self.assertIn("\x1b[?25h", output)
        self.assertIn("Code Mule", output)
        self.assertIn("Progress", output)
        self.assertTrue(renderer.closed)
        self.assertFalse(renderer.thread_alive)

    def test_human_gate_stops_running_display(self):
        stream = FakeTTY()
        renderer = ConsoleProgressRenderer(
            stream, refresh_interval=0.05, monotonic=lambda: 1.0
        )
        with renderer:
            renderer.emit(
                event(
                    ProgressEventType.HUMAN_GATE,
                    task_id="task-1",
                    message="Approval required",
                    metadata={"source": "worker_failure"},
                )
            )
        self.assertEqual(renderer.snapshot.project_status, "human_required")
        self.assertIn("! ACTION REQUIRED", stream.getvalue())

    def test_planning_has_no_fake_percentage_and_materialization_sets_task_total(self):
        stream = FakeTTY()
        renderer = ConsoleProgressRenderer(
            stream, refresh_interval=0.05, monotonic=lambda: 1.0
        )
        with renderer:
            renderer.emit(
                event(
                    ProgressEventType.PLANNING_STARTED,
                    message="Planning started",
                )
            )
            renderer.emit(
                event(
                    ProgressEventType.SUPERVISOR_PLAN_STARTED,
                    second=1,
                    message="Supervisor planning",
                )
            )
            self.assertEqual(renderer.snapshot.project_status, "planning")
            self.assertEqual(renderer.snapshot.total_tasks, 0)
            sleep(0.06)
            renderer.emit(
                event(
                    ProgressEventType.PLANNING_COMPLETED,
                    second=2,
                    message="Plan v1 created with 2 tasks",
                    metadata={"total_tasks": "2"},
                )
            )
        output = stream.getvalue()
        self.assertIn("Progress    Planning", output)
        self.assertEqual(renderer.snapshot.project_status, "running")
        self.assertEqual(renderer.snapshot.total_tasks, 2)
        self.assertEqual(renderer.snapshot.percentage, 0.0)

    def test_change_replanning_progress_returns_display_to_running(self):
        renderer = ConsoleProgressRenderer(StringIO(), monotonic=lambda: 1.0)
        renderer.emit(
            event(
                ProgressEventType.CHANGE_REQUESTED,
                message="Change requested",
            )
        )
        renderer.emit(
            event(
                ProgressEventType.REPLANNING_STARTED,
                second=1,
                message="Change replanning started",
            )
        )
        renderer.emit(
            event(
                ProgressEventType.SUPERVISOR_IMPACT_STARTED,
                second=2,
                message="Supervisor analyzing impact...",
            )
        )
        self.assertEqual(renderer.snapshot.project_status, "replanning")
        self.assertEqual(renderer.snapshot.supervisor_status, "Analyzing impact")
        renderer.emit(
            event(
                ProgressEventType.REPLANNING_COMPLETED,
                second=3,
                message="Plan v1 → v2; execution resumed",
            )
        )
        self.assertEqual(renderer.snapshot.project_status, "running")
        self.assertEqual(renderer.snapshot.supervisor_status, "Completed")


if __name__ == "__main__":
    unittest.main()
