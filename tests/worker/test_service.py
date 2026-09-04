import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import ANY

from code_mule.worker.contracts import (
    CodexProtocolError,
    CodexWorkerConfig,
    WorkerTaskRequest,
    WorkerTurnResult,
)
from code_mule.worker.service import CodexWorkerService

from .test_contracts import make_task


class FakeClient:
    def __init__(self, result=None, failure=None):
        self.result = result or WorkerTurnResult(
            "thread-1",
            "turn-1",
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Completed",
                    "files_changed": [],
                    "tests": [],
                    "static_checks": [],
                    "git_state": "unknown",
                    "issues": [],
                    "human_action_required": False,
                }
            ),
            True,
            3,
            (),
        )
        self.failure = failure
        self.calls = []
        self.closed = False

    def initialize(self):
        self.calls.append(("initialize",))
        if self.failure is not None:
            raise self.failure

    def start_thread(self):
        self.calls.append(("start_thread",))
        return "thread-1"

    def start_turn(self, thread_id, prompt, *, output_schema=None):
        self.calls.append(("start_turn", thread_id, prompt, output_schema))
        return "turn-1"

    def wait_for_turn(self, thread_id, turn_id):
        self.calls.append(("wait_for_turn", thread_id, turn_id))
        return self.result

    def close(self):
        self.calls.append(("close",))
        self.closed = True


def worker_config():
    return CodexWorkerConfig(
        command=("codex", "app-server"),
        workspace=Path("/tmp/project"),
        approval_policy="on-request",
        sandbox="read-only",
        inactivity_timeout_seconds=30,
        max_turn_seconds=120,
    )


class CodexWorkerServiceTests(unittest.TestCase):
    def test_execute_runs_lifecycle_maps_report_and_closes(self):
        fake = FakeClient()
        captured = []

        def factory(config):
            captured.append(config)
            return fake

        service = CodexWorkerService(worker_config(), client_factory=factory)
        task = make_task()
        request = WorkerTaskRequest(task, "Inspect files", "Read-only")
        created_at = datetime.now(UTC)

        report = service.execute(
            request,
            report_id="report-1",
            created_at=created_at,
        )

        self.assertEqual(captured, [worker_config()])
        self.assertEqual(
            fake.calls,
            [
                ("initialize",),
                ("start_thread",),
                ("start_turn", "thread-1", "Inspect files", ANY),
                ("wait_for_turn", "thread-1", "turn-1"),
                ("close",),
            ],
        )
        self.assertTrue(fake.closed)
        self.assertEqual(report.id, "report-1")
        self.assertEqual(report.task_id, task.id)
        self.assertEqual(report.summary, "Completed")
        self.assertEqual(task.execution_attempts, 2)

    def test_execute_closes_client_when_protocol_fails(self):
        failure = CodexProtocolError("invalid response")
        fake = FakeClient(failure=failure)
        service = CodexWorkerService(worker_config(), client_factory=lambda _: fake)
        request = WorkerTaskRequest(make_task(), "Inspect", "Read-only")

        with self.assertRaises(CodexProtocolError) as raised:
            service.execute(
                request,
                report_id="report-1",
                created_at=datetime.now(UTC),
            )

        self.assertIs(raised.exception, failure)
        self.assertTrue(fake.closed)
        self.assertEqual(fake.calls, [("initialize",), ("close",)])

    def test_execute_closes_client_when_boss_interrupts_active_turn(self):
        class InterruptingClient(FakeClient):
            def wait_for_turn(self, thread_id, turn_id):
                self.calls.append(("wait_for_turn", thread_id, turn_id))
                raise KeyboardInterrupt

        fake = InterruptingClient()
        service = CodexWorkerService(worker_config(), client_factory=lambda _: fake)
        request = WorkerTaskRequest(make_task(), "Inspect", "Read-only")

        with self.assertRaises(KeyboardInterrupt):
            service.execute(
                request,
                report_id="report-1",
                created_at=datetime.now(UTC),
            )

        self.assertTrue(fake.closed)
        self.assertEqual(fake.calls[-1], ("close",))


if __name__ == "__main__":
    unittest.main()
