"""Atomic in-memory materialization of one validated replacement Plan."""

from dataclasses import replace
from datetime import datetime

from code_mule.domain.enums import (
    ChangeRequestStatus,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    TaskStatus,
)
from code_mule.domain.models import (
    ChangeRequest,
    ImpactAnalysis,
    Milestone,
    Plan,
    Requirement,
    Task,
)
from code_mule.domain.state_machine import validate_transition
from code_mule.scheduler import SchedulerError, TaskScheduler
from code_mule.scheduler.selection import resolve_active_plan_graph
from code_mule.state.models import ProjectState
from code_mule.supervisor.contracts import (
    ImpactAnalysisResult,
    RequirementProposal,
)

from .errors import ReplanMaterializationError
from .validation import ChangeReplanValidator


class ChangeReplanMaterializer:
    """Build a complete RUNNING snapshot without performing persistence."""

    def __init__(
        self,
        validator: ChangeReplanValidator,
        scheduler: TaskScheduler | None = None,
    ) -> None:
        self._validator = validator
        self._scheduler = scheduler or TaskScheduler()

    def materialize(
        self,
        state: ProjectState,
        change_request: ChangeRequest,
        proposal: ImpactAnalysisResult,
        *,
        plan_id: str,
        operation_time: datetime,
    ) -> ProjectState:
        if plan_id == "":
            raise ReplanMaterializationError("plan_id must not be empty")
        if (
            state.project.status is not ProjectStatus.REPLANNING
            or state.project.current_task_id is not None
            or change_request.status is not ChangeRequestStatus.ANALYZING
        ):
            raise ReplanMaterializationError(
                "materialization requires a safe REPLANNING Project"
            )
        self._validator.validate(state, change_request, proposal)
        graph = resolve_active_plan_graph(state)

        occupied_ids = {
            state.project.id,
            *(item.id for item in state.requirements),
            *(item.id for item in state.plans),
            *(item.id for item in state.milestones),
            *(item.id for item in state.tasks),
            *(item.id for item in state.change_requests),
            *(item.id for item in proposal.requirements_to_add),
            *(item.requirement.id for item in proposal.requirements_to_update),
            *(item.id for item in proposal.tasks_to_add),
            *(item.id for item in proposal.milestones),
        }
        if plan_id in occupied_ids:
            raise ReplanMaterializationError(
                f"plan_id collides with existing or proposed history: {plan_id}"
            )

        superseded_ids = {
            item.supersedes_id for item in proposal.requirements_to_update
        }
        requirements = tuple(
            replace(
                item,
                status=RequirementStatus.SUPERSEDED,
                updated_at=operation_time,
            )
            if item.id in superseded_ids
            else item
            for item in state.requirements
        )
        requirements += tuple(
            self._requirement(
                state,
                change_request,
                item,
                operation_time,
                supersedes_id=None,
            )
            for item in proposal.requirements_to_add
        )
        requirements += tuple(
            self._requirement(
                state,
                change_request,
                item.requirement,
                operation_time,
                supersedes_id=item.supersedes_id,
            )
            for item in proposal.requirements_to_update
        )

        cancelled = set(proposal.tasks_to_cancel)
        reopened = set(proposal.tasks_to_reopen)
        active_task_ids = tuple(
            item.id for item in graph.tasks if item.id not in cancelled
        ) + tuple(item.id for item in proposal.tasks_to_add)
        reused_milestones = tuple(
            item
            for milestone_id in proposal.milestone_ids_reused
            for item in state.milestones
            if item.id == milestone_id
        )
        task_milestones = {
            task_id: milestone.id
            for milestone in reused_milestones + proposal.milestones
            for task_id in milestone.task_ids
        }
        dependency_changes = {
            item.task_id: item.dependencies for item in proposal.dependency_changes
        }
        task_requirement_updates = {
            item.task_id: item.requirement_ids
            for item in proposal.task_requirement_updates
        }
        active_ids = set(active_task_ids)
        tasks: tuple[Task, ...] = ()
        for task in state.tasks:
            if task.id in cancelled:
                tasks += (
                    replace(
                        task,
                        status=TaskStatus.CANCELLED,
                        updated_at=operation_time,
                    ),
                )
            elif task.id in active_ids:
                tasks += (
                    replace(
                        task,
                        milestone_id=task_milestones[task.id],
                        status=(
                            TaskStatus.REOPENED
                            if task.id in reopened
                            else task.status
                        ),
                        dependencies=dependency_changes.get(
                            task.id, task.dependencies
                        ),
                        requirement_ids=task_requirement_updates.get(
                            task.id, task.requirement_ids
                        ),
                        updated_at=operation_time,
                    ),
                )
            else:
                tasks += (task,)
        tasks += tuple(
            Task(
                id=item.id,
                milestone_id=task_milestones[item.id],
                title=item.title,
                description=item.description,
                status=TaskStatus.PENDING,
                dependencies=item.dependencies,
                acceptance_criteria=item.acceptance_criteria,
                execution_attempts=0,
                created_at=operation_time,
                updated_at=operation_time,
                requirement_ids=item.requirement_ids,
            )
            for item in proposal.tasks_to_add
        )
        tasks_by_id = {item.id: item for item in tasks}

        plan_version = max(item.version for item in state.plans) + 1
        new_milestones = tuple(
            Milestone(
                id=item.id,
                plan_id=plan_id,
                title=item.title,
                status=(
                    "completed"
                    if all(
                        tasks_by_id[task_id].status is TaskStatus.COMPLETED
                        for task_id in item.task_ids
                    )
                    else "pending"
                ),
                task_ids=item.task_ids,
            )
            for item in proposal.milestones
        )
        reused_ids = set(proposal.milestone_ids_reused)
        milestones = tuple(
            replace(
                item,
                plan_id=plan_id,
                status=(
                    "completed"
                    if all(
                        tasks_by_id[task_id].status is TaskStatus.COMPLETED
                        for task_id in item.task_ids
                    )
                    else "pending"
                ),
            )
            if item.id in reused_ids
            else item
            for item in state.milestones
        ) + new_milestones
        old_requirement_ids = tuple(
            item
            for item in graph.plan.requirement_ids
            if item not in superseded_ids
        )
        plan = Plan(
            id=plan_id,
            project_id=state.project.id,
            version=plan_version,
            status=PlanStatus.ACTIVE,
            requirement_ids=(
                old_requirement_ids
                + tuple(item.id for item in proposal.requirements_to_add)
                + tuple(
                    item.requirement.id
                    for item in proposal.requirements_to_update
                )
            ),
            milestone_ids=(
                proposal.milestone_ids_reused
                + tuple(item.id for item in new_milestones)
            ),
            created_at=operation_time,
        )
        plans = tuple(
            replace(item, status=PlanStatus.SUPERSEDED)
            if item.id == graph.plan.id
            else item
            for item in state.plans
        ) + (plan,)
        affected_requirements = tuple(
            dict.fromkeys(
                proposal.affected_requirement_ids
                + tuple(item.id for item in proposal.requirements_to_add)
                + tuple(
                    item.supersedes_id
                    for item in proposal.requirements_to_update
                )
                + tuple(
                    item.requirement.id
                    for item in proposal.requirements_to_update
                )
            )
        )
        applied_change = replace(
            change_request,
            status=ChangeRequestStatus.APPLIED,
            affected_requirement_ids=affected_requirements,
        )
        impact = self._impact(proposal)
        validate_transition(state.project.status, ProjectStatus.RUNNING)
        materialized = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.RUNNING,
                active_plan_id=plan.id,
                current_task_id=None,
                updated_at=operation_time,
            ),
            requirements=requirements,
            plans=plans,
            milestones=milestones,
            tasks=tasks,
            change_requests=tuple(
                applied_change if item.id == applied_change.id else item
                for item in state.change_requests
            ),
            impact_analyses=state.impact_analyses + (impact,),
        )
        try:
            self._scheduler.validate(materialized)
        except SchedulerError as error:
            raise ReplanMaterializationError(
                "materialized replacement Plan is not schedulable"
            ) from error
        return materialized

    @staticmethod
    def _requirement(
        state: ProjectState,
        change_request: ChangeRequest,
        proposal: RequirementProposal,
        operation_time: datetime,
        *,
        supersedes_id: str | None,
    ) -> Requirement:
        return Requirement(
            id=proposal.id,
            project_id=state.project.id,
            title=proposal.title,
            description=proposal.description,
            status=RequirementStatus.ACTIVE,
            priority=proposal.priority,
            acceptance_criteria=proposal.acceptance_criteria,
            introduced_by=change_request.id,
            created_at=operation_time,
            updated_at=operation_time,
            supersedes_id=supersedes_id,
        )

    @staticmethod
    def _impact(proposal: ImpactAnalysisResult) -> ImpactAnalysis:
        return ImpactAnalysis(
            change_request_id=proposal.change_request_id,
            architecture_impact=proposal.architecture_impact,
            affected_components=proposal.affected_components,
            affected_completed_tasks=proposal.affected_completed_tasks,
            affected_in_progress_tasks=proposal.affected_in_progress_tasks,
            affected_pending_tasks=proposal.affected_pending_tasks,
            tasks_to_add=tuple(item.id for item in proposal.tasks_to_add),
            tasks_to_reopen=proposal.tasks_to_reopen,
            tasks_to_cancel=proposal.tasks_to_cancel,
            recommendation=proposal.recommendation,
            summary=proposal.summary,
            affected_requirement_ids=proposal.affected_requirement_ids,
            affected_task_ids=proposal.affected_task_ids,
            requirements_to_add=tuple(
                item.id for item in proposal.requirements_to_add
            ),
            requirements_to_update=tuple(
                f"{item.supersedes_id}->{item.requirement.id}"
                for item in proposal.requirements_to_update
            ),
            milestone_ids=(
                proposal.milestone_ids_reused
                + tuple(item.id for item in proposal.milestones)
            ),
            dependency_changes=tuple(
                f"{item.task_id}:{','.join(item.dependencies)}"
                for item in proposal.dependency_changes
            ),
            risks=proposal.risks,
            rationale=proposal.rationale,
        )


__all__ = ["ChangeReplanMaterializer"]
