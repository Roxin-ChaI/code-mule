import unittest
from datetime import UTC, datetime

from code_mule.worker.contracts import WorkerTaskRequest, WorkerTurnResult
from code_mule.worker.parsing import build_execution_report

from .test_contracts import make_task


class ExecutionReportMappingTests(unittest.TestCase):
    def test_maps_only_structured_worker_facts(self):
        task = make_task()
        request = WorkerTaskRequest(task, "Run task", "Task")
        result = WorkerTurnResult(
            "thread-1",
            "turn-1",
            "Tests passed; changed src/claimed.py; git clean.",
            True,
            4,
            ("structured issue",),
        )
        created_at = datetime.now(UTC)

        report = build_execution_report(request, result, "report-1", created_at)

        self.assertEqual(report.task_id, task.id)
        self.assertEqual(report.attempt, 3)
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.summary, result.final_message)
        self.assertEqual(report.issues, ("structured issue",))
        self.assertEqual(report.files_changed, ())
        self.assertEqual(report.tests, ())
        self.assertEqual(report.static_checks, ())
        self.assertEqual(report.git_state, "unknown")
        self.assertFalse(report.human_action_required)
        self.assertIs(report.created_at, created_at)
        self.assertEqual(task.execution_attempts, 2)

    def test_missing_final_message_has_safe_non_evidentiary_fallback(self):
        request = WorkerTaskRequest(make_task(), "Run", "Task")
        result = WorkerTurnResult("thread", "turn", None, True, 1, ())
        report = build_execution_report(
            request, result, "report", datetime.now(UTC)
        )
        self.assertEqual(
            report.summary,
            "Codex turn completed without a final agent message.",
        )

    def test_explicit_non_completion_maps_to_failed(self):
        request = WorkerTaskRequest(make_task(), "Run", "Task")
        result = WorkerTurnResult("thread", "turn", None, False, 1, ("failed",))
        report = build_execution_report(
            request, result, "report", datetime.now(UTC)
        )
        self.assertEqual(report.status, "failed")


if __name__ == "__main__":
    unittest.main()
