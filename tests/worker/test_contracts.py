import unittest
from datetime import UTC, datetime
from pathlib import Path

from code_mule.domain.enums import TaskStatus
from code_mule.domain.models import Task
from code_mule.worker.contracts import (
    CodexApprovalRequired,
    CodexProtocolError,
    CodexWorkerConfig,
    CodexWorkerError,
    WorkerTaskRequest,
    WorkerTurnResult,
)


def make_task() -> Task:
    now = datetime.now(UTC)
    return Task(
        id="task-1",
        milestone_id="milestone-1",
        title="Worker",
        description="Execute a task",
        status=TaskStatus.PENDING,
        dependencies=(),
        acceptance_criteria=("report result",),
        execution_attempts=2,
        created_at=now,
        updated_at=now,
    )


class WorkerContractTests(unittest.TestCase):
    def test_config_preserves_explicit_local_runtime_settings(self):
        config = CodexWorkerConfig(
            command=("codex", "app-server"),
            workspace=Path("/tmp/project"),
            approval_policy="on-request",
            sandbox="read-only",
            inactivity_timeout_seconds=30,
            max_turn_seconds=120,
        )
        self.assertEqual(config.command, ("codex", "app-server"))
        self.assertEqual(config.workspace, Path("/tmp/project"))
        self.assertFalse(hasattr(config, "api_key"))

    def test_config_rejects_invalid_command_workspace_and_timeout(self):
        valid = {
            "command": ("codex", "app-server"),
            "workspace": Path("/tmp/project"),
            "approval_policy": "on-request",
            "sandbox": "read-only",
            "inactivity_timeout_seconds": 30,
            "max_turn_seconds": 120,
        }
        for field, value in (
            ("command", ()),
            ("command", ("codex", "")),
            ("workspace", Path("relative")),
            ("approval_policy", ""),
            ("sandbox", ""),
            ("inactivity_timeout_seconds", 0),
            ("inactivity_timeout_seconds", -1),
            ("max_turn_seconds", 0),
            ("max_turn_seconds", -1),
        ):
            case = dict(valid)
            case[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    CodexWorkerConfig(**case)
        with self.assertRaisesRegex(ValueError, "at least"):
            CodexWorkerConfig(
                command=("codex", "app-server"),
                workspace=Path("/tmp/project"),
                approval_policy="on-request",
                sandbox="read-only",
                inactivity_timeout_seconds=31,
                max_turn_seconds=30,
            )

    def test_task_request_keeps_task_immutable_at_boundary(self):
        task = make_task()
        request = WorkerTaskRequest(task, "Inspect files", "Read-only inspection")
        self.assertIs(request.task, task)
        self.assertEqual(task.execution_attempts, 2)
        with self.assertRaises(ValueError):
            WorkerTaskRequest(task, "", "title")
        with self.assertRaises(ValueError):
            WorkerTaskRequest(task, "prompt", "")

    def test_turn_result_validation(self):
        result = WorkerTurnResult("thread-1", "turn-1", None, True, 0, ())
        self.assertTrue(result.completed)
        for values in (
            ("", "turn", 0),
            ("thread", "", 0),
            ("thread", "turn", -1),
        ):
            with self.assertRaises(ValueError):
                WorkerTurnResult(values[0], values[1], None, True, values[2], ())

    def test_boundary_errors_share_one_public_base(self):
        self.assertTrue(issubclass(CodexProtocolError, CodexWorkerError))
        self.assertTrue(issubclass(CodexApprovalRequired, CodexWorkerError))


if __name__ == "__main__":
    unittest.main()
