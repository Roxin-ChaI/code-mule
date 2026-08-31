import unittest
from datetime import UTC, datetime

from code_mule.progress import ProgressEvent, ProgressEventType


NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


class ProgressContractTests(unittest.TestCase):
    def test_event_types_use_stable_dot_style_values(self):
        self.assertEqual(ProgressEventType.PROJECT_STARTED, "project.started")
        self.assertEqual(ProgressEventType.WORKER_ACTIVITY, "worker.activity")
        self.assertEqual(
            ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
            "supervisor.review_completed",
        )
        self.assertEqual(ProgressEventType.HUMAN_GATE, "runtime.human_gate")

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


if __name__ == "__main__":
    unittest.main()
