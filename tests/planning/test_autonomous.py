import unittest

from code_mule.domain.enums import ProjectStatus
from code_mule.planning import (
    AutonomousProjectService,
    ProjectPlanningOutcome,
    ProjectPlanningRequest,
)
from code_mule.runtime import ProjectExecutionOutcome, ProjectExecutionStopReason


class FakePlanning:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def plan(self, request):
        self.requests.append(request)
        return self.outcome


class FakeExecution:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def run(self):
        self.calls += 1
        return self.outcome


def planning_outcome(*, ready=True, status=ProjectStatus.RUNNING):
    return ProjectPlanningOutcome(
        project_id="project-1",
        plan_id="plan-1",
        plan_version=1,
        requirement_ids=("REQ-001",),
        milestone_ids=("M1",),
        task_ids=("T1", "T2"),
        project_status=status,
        ready_for_execution=ready,
    )


def execution_outcome():
    return ProjectExecutionOutcome(
        project_id="project-1",
        tasks_started=2,
        tasks_completed=2,
        task_ids=("T1", "T2"),
        final_project_status=ProjectStatus.DONE,
        plan_completed=True,
        human_action_required=False,
        stop_reason=ProjectExecutionStopReason.PLAN_COMPLETED,
    )


class AutonomousProjectServiceTests(unittest.TestCase):
    def test_ready_plan_runs_execution_once_and_combines_outcome(self):
        planning = FakePlanning(planning_outcome())
        execution = FakeExecution(execution_outcome())
        service = AutonomousProjectService(
            planning_service=planning,
            execution_service=execution,
        )
        request = ProjectPlanningRequest("project-1", "Build calculator")

        outcome = service.run_new_project(request)

        self.assertEqual(planning.requests, [request])
        self.assertEqual(execution.calls, 1)
        self.assertIs(outcome.final_project_status, ProjectStatus.DONE)
        self.assertIs(outcome.execution, execution.outcome)
        self.assertFalse(outcome.human_action_required)

    def test_not_ready_planning_never_calls_execution(self):
        planning = FakePlanning(
            planning_outcome(ready=False, status=ProjectStatus.HUMAN_REQUIRED)
        )
        execution = FakeExecution(execution_outcome())
        service = AutonomousProjectService(
            planning_service=planning,
            execution_service=execution,
        )

        outcome = service.run_new_project(
            ProjectPlanningRequest("project-1", "Build calculator")
        )

        self.assertEqual(execution.calls, 0)
        self.assertIsNone(outcome.execution)
        self.assertIs(
            outcome.final_project_status, ProjectStatus.HUMAN_REQUIRED
        )
        self.assertTrue(outcome.human_action_required)


if __name__ == "__main__":
    unittest.main()
