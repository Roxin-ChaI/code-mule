import unittest
from datetime import UTC, datetime

from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSnapshot,
    progress_percentage,
)


NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


class ProgressContractTests(unittest.TestCase):
    def test_event_types_use_stable_dot_style_values(self):
        self.assertEqual(ProgressEventType.PROJECT_STARTED, "project.started")
        self.assertEqual(ProgressEventType.WORKER_ACTIVITY, "worker.activity")
        self.assertEqual(ProgressEventType.PLANNING_STARTED, "planning.started")
        self.assertEqual(ProgressEventType.CHANGE_REQUESTED, "change.requested")
        self.assertEqual(
            ProgressEventType.REPLANNING_COMPLETED,
            "replanning.completed",
        )
        self.assertEqual(
            ProgressEventType.SUPERVISOR_PLAN_STARTED,
            "supervisor.plan_started",
        )
        self.assertEqual(
            ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
            "supervisor.review_completed",
        )
        self.assertEqual(ProgressEventType.HUMAN_GATE, "runtime.human_gate")
        self.assertEqual(
            ProgressEventType.PROJECT_CANCELLATION_REQUESTED,
            "project.cancel_requested",
        )
        self.assertEqual(ProgressEventType.PROJECT_CANCELLED, "project.cancelled")

    def test_event_preserves_caller_timestamp_and_typed_metadata(self):
        event = ProgressEvent(
            ProgressEventType.TASK_STARTED,
            NOW,
            "project-1",
            "task-1",
            2,
            "Task started",
            {"task_title": "Build feature"},
        )
        self.assertEqual(event.timestamp, NOW)
        self.assertEqual(event.attempt, 2)
        self.assertEqual(event.metadata, {"task_title": "Build feature"})
        with self.assertRaises(ValueError):
            ProgressEvent(
                ProgressEventType.TASK_STARTED,
                NOW,
                None,
                None,
                0,
                None,
                {},
            )
        with self.assertRaises(TypeError):
            ProgressEvent(
                ProgressEventType.ERROR,
                NOW,
                None,
                None,
                None,
                None,
                {"count": 1},  # type: ignore[dict-item]
            )

    def test_progress_percentage_uses_only_completed_over_total(self):
        self.assertEqual(progress_percentage(0, 4), 0.0)
        self.assertEqual(progress_percentage(1, 4), 25.0)
        self.assertEqual(progress_percentage(2, 4), 50.0)
        self.assertEqual(progress_percentage(4, 4), 100.0)
        self.assertEqual(progress_percentage(0, 0), 0.0)
        for completed, total in ((-1, 4), (1, -1), (5, 4)):
            with self.subTest(completed=completed, total=total):
                with self.assertRaises(ValueError):
                    progress_percentage(completed, total)

    def test_snapshot_percentage_does_not_use_attempt_or_elapsed_time(self):
        snapshot = ProgressSnapshot(
            project_id="project-1",
            project_status="running",
            completed_tasks=1,
            total_tasks=4,
            current_task_id="task-2",
            current_task_title="Task two",
            current_attempt=99,
            worker_status="working",
            supervisor_status="waiting",
            project_started_at=NOW,
            task_started_at=NOW,
            stage_started_at=NOW,
            recent_events=(),
        )
        self.assertEqual(snapshot.percentage, 25.0)


if __name__ == "__main__":
    unittest.main()
