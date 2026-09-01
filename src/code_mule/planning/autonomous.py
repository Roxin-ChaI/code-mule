"""Minimal composition of independently testable planning and execution."""

from typing import Protocol

from code_mule.runtime.contracts import ProjectExecutionOutcome

from .contracts import (
    AutonomousProjectOutcome,
    ProjectPlanningOutcome,
    ProjectPlanningRequest,
)


class PlanningRunner(Protocol):
    def plan(self, request: ProjectPlanningRequest) -> ProjectPlanningOutcome: ...


class ProjectExecutionRunner(Protocol):
    def run(self) -> ProjectExecutionOutcome: ...


class AutonomousProjectService:
    """Compose initial planning and execution without adding reasoning."""

    def __init__(
        self,
        *,
        planning_service: PlanningRunner,
        execution_service: ProjectExecutionRunner,
    ) -> None:
        self._planning_service = planning_service
        self._execution_service = execution_service

    def run_new_project(
        self, request: ProjectPlanningRequest
    ) -> AutonomousProjectOutcome:
        planning = self._planning_service.plan(request)
        if not planning.ready_for_execution:
            return AutonomousProjectOutcome(
                planning=planning,
                execution=None,
                final_project_status=planning.project_status,
                human_action_required=True,
            )
        execution = self._execution_service.run()
        return AutonomousProjectOutcome(
            planning=planning,
            execution=execution,
            final_project_status=execution.final_project_status,
            human_action_required=execution.human_action_required,
        )


__all__ = [
    "AutonomousProjectService",
    "PlanningRunner",
    "ProjectExecutionRunner",
]
