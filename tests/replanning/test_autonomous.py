import unittest

from code_mule.domain.enums import ProjectStatus
from code_mule.replanning import (
    ChangeExecutionService,
    ChangeReplanningOutcome,
    ChangeReplanningRequest,
)
from code_mule.runtime import ProjectExecutionOutcome, ProjectExecutionStopReason


class FakeReplanning:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome
        self.error = error
        self.requests = []

    def replan(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.outcome


class FakeExecution:
    def __init__(self):
        self.calls = 0

    def run(self):
        self.calls += 1
        return ProjectExecutionOutcome(
            "project-1",
            1,
            1,
            ("T3",),
            ProjectStatus.DONE,
            True,
            False,
            ProjectExecutionStopReason.PLAN_COMPLETED,
        )


class ChangeExecutionServiceTests(unittest.TestCase):
    def test_replanning_failure_never_starts_execution(self):
        replanning = FakeReplanning(error=OSError("materialization save failed"))
        execution = FakeExecution()
        composition = ChangeExecutionService(
            replanning_service=replanning,
            execution_service=execution,
        )

        with self.assertRaises(OSError):
            composition.apply_and_resume(
                ChangeReplanningRequest("project-1", "CHANGE-1")
            )

        self.assertEqual(execution.calls, 0)

    def test_replans_then_continues_execution(self):
        replanning = FakeReplanning(
            ChangeReplanningOutcome(
                "project-1",
                "CHANGE-1",
                "PLAN-1",
                "PLAN-2",
                1,
                2,
                ProjectStatus.RUNNING,
                (),
                (),
                ("T3",),
                True,
            )
        )
        execution = FakeExecution()
        composition = ChangeExecutionService(
            replanning_service=replanning,
            execution_service=execution,
        )

        outcome = composition.apply_and_resume(
            ChangeReplanningRequest("project-1", "CHANGE-1")
        )

        self.assertEqual(execution.calls, 1)
        self.assertIs(outcome.final_project_status, ProjectStatus.DONE)
        self.assertEqual(outcome.execution.task_ids, ("T3",))


if __name__ == "__main__":
    unittest.main()
