"""Composition of safe-point replanning and resumed sequential execution."""

from typing import Protocol

from code_mule.runtime.contracts import ProjectExecutionOutcome

from .contracts import (
    ChangeExecutionOutcome,
    ChangeReplanningOutcome,
    ChangeReplanningRequest,
)


class ReplanningRunner(Protocol):
    def replan(
        self, request: ChangeReplanningRequest
    ) -> ChangeReplanningOutcome: ...


class ProjectExecutionRunner(Protocol):
    def run(self) -> ProjectExecutionOutcome: ...


class ChangeExecutionService:
    """Resume execution only after a replacement Plan is durably RUNNING."""

    def __init__(
        self,
        *,
        replanning_service: ReplanningRunner,
        execution_service: ProjectExecutionRunner,
    ) -> None:
        self._replanning_service = replanning_service
        self._execution_service = execution_service

    def apply_and_resume(
        self, request: ChangeReplanningRequest
    ) -> ChangeExecutionOutcome:
        replanning = self._replanning_service.replan(request)
        if not replanning.ready_for_execution:
            return ChangeExecutionOutcome(
                replanning=replanning,
                execution=None,
                final_project_status=replanning.project_status,
                human_action_required=True,
            )
        execution = self._execution_service.run()
        return ChangeExecutionOutcome(
            replanning=replanning,
            execution=execution,
            final_project_status=execution.final_project_status,
            human_action_required=execution.human_action_required,
        )


__all__ = [
    "ChangeExecutionService",
    "ProjectExecutionRunner",
    "ReplanningRunner",
]
