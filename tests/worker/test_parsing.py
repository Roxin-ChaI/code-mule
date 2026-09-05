import unittest
from datetime import UTC, datetime

from code_mule.domain.enums import WorkerHumanActionKind
from code_mule.domain.models import WorkerHumanAction
from code_mule.worker.contracts import WorkerTaskRequest
from code_mule.worker.parsing import build_execution_report
from code_mule.worker.structured_report import (
    StructuredWorkerReport,
    WorkerCheckResult,
    WorkerCheckStatus,
    WorkerExecutionStatus,
)

from .test_contracts import make_task


class ExecutionReportMappingTests(unittest.TestCase):
    def test_maps_all_and_only_validated_structured_evidence(self):
        task = make_task()
        request = WorkerTaskRequest(task, "Run task", "Task")
        result = StructuredWorkerReport(
            status=WorkerExecutionStatus.COMPLETED,
            summary="Implemented the fix",
            files_changed=("src/b.py", "src/a.py"),
            tests=(
                WorkerCheckResult(
                    "python -m unittest", WorkerCheckStatus.PASS, "129 passed"
                ),
                WorkerCheckResult("integration", WorkerCheckStatus.NOT_RUN, None),
            ),
            static_checks=(
                WorkerCheckResult("compileall", WorkerCheckStatus.PASS, None),
            ),
            git_state="dirty",
            issues=("report issue",),
            human_action=None,
        )
        created_at = datetime.now(UTC)

        report = build_execution_report(
            request,
            result,
            "report-1",
            created_at,
            transport_issues=("transport warning",),
        )

        self.assertEqual(report.task_id, task.id)
        self.assertEqual(report.attempt, 3)
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.summary, "Implemented the fix")
        self.assertEqual(report.files_changed, ("src/b.py", "src/a.py"))
        self.assertEqual(
            report.tests,
            (
                "python -m unittest: pass (129 passed)",
                "integration: not_run",
            ),
        )
        self.assertEqual(report.static_checks, ("compileall: pass",))
        self.assertEqual(report.git_state, "dirty")
        self.assertEqual(report.issues, ("transport warning", "report issue"))
        self.assertFalse(report.human_action_required)
        self.assertIs(report.created_at, created_at)
        self.assertEqual(task.execution_attempts, 2)

    def test_structured_failed_and_blocked_statuses_are_preserved(self):
        request = WorkerTaskRequest(make_task(), "Run", "Task")
        for status in (WorkerExecutionStatus.FAILED, WorkerExecutionStatus.BLOCKED):
            with self.subTest(status=status):
                result = StructuredWorkerReport(
                    status,
                    "Stopped",
                    (),
                    (),
                    (),
                    "unknown",
                    (),
                    WorkerHumanAction(
                        WorkerHumanActionKind.INPUT,
                        "Input required",
                        "Choose an option",
                    ),
                )
                report = build_execution_report(
                    request, result, "report", datetime.now(UTC)
                )
                self.assertEqual(report.status, status.value)
                self.assertTrue(report.human_action_required)


if __name__ == "__main__":
    unittest.main()
