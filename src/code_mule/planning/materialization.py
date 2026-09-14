"""Materialize a validated proposal into domain entities in memory."""

from dataclasses import replace
from datetime import datetime

from code_mule.domain.enums import (
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    TaskStatus,
)
from code_mule.domain.models import Milestone, Plan, Requirement, Task
from code_mule.domain.state_machine import validate_transition
from code_mule.state.models import ProjectState
from code_mule.supervisor.contracts import PlanProposal
from code_mule.revision import begin_revision

from .errors import PlanMaterializationError, PlanningValidationCode
from .validation import PlanProposalValidator


class PlanMaterializer:
    """Build one atomic RUNNING snapshot without performing persistence."""

    def __init__(self, validator: PlanProposalValidator) -> None:
        self._validator = validator

    def materialize(
        self,
        state: ProjectState,
        proposal: PlanProposal,
        *,
        plan_id: str,
        operation_time: datetime,
    ) -> ProjectState:
        if plan_id == "":
            raise PlanMaterializationError(
                "Code Mule must generate a non-empty Plan ID",
                code=PlanningValidationCode.PLAN_ID_INVALID,
                field_path="plan_id",
            )
        if (
            state.project.status is not ProjectStatus.PLANNING
            or state.project.active_plan_id is not None
            or state.project.current_task_id is not None
            or any(plan.status is PlanStatus.ACTIVE for plan in state.plans)
        ):
            raise PlanMaterializationError(
                "Materialization requires an unbound PLANNING Project",
                code=PlanningValidationCode.MATERIALIZATION_STATE,
                field_path="project.status",
            )
        self._validator.validate(state, proposal)

        occupied_ids = {
            state.project.id,
            *(item.id for item in state.requirements),
            *(item.id for item in state.plans),
            *(item.id for item in state.milestones),
            *(item.id for item in state.tasks),
            *(item.id for item in state.change_requests),
            *(item.id for item in proposal.requirements),
            *(item.id for item in proposal.milestones),
            *(item.id for item in proposal.tasks),
        }
        if plan_id in occupied_ids:
            raise PlanMaterializationError(
                "The Code Mule-owned Plan ID collides with another entity",
                code=PlanningValidationCode.PLAN_ID_COLLISION,
                field_path="plan_id",
            )
        try:
            requirements = tuple(
                Requirement(
                    id=item.id,
                    project_id=state.project.id,
                    title=item.title,
                    description=item.description,
                    status=RequirementStatus.ACTIVE,
                    priority=item.priority,
                    acceptance_criteria=item.acceptance_criteria,
                    introduced_by="boss",
                    created_at=operation_time,
                    updated_at=operation_time,
                )
                for item in proposal.requirements
            )
            task_milestones = {
                task_id: milestone.id
                for milestone in proposal.milestones
                for task_id in milestone.task_ids
            }
            tasks = tuple(
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
                for item in proposal.tasks
            )
            milestones = tuple(
                Milestone(
                    id=item.id,
                    plan_id=plan_id,
                    title=item.title,
                    status="pending",
                    task_ids=item.task_ids,
                )
                for item in proposal.milestones
            )
            plan_version = max((item.version for item in state.plans), default=0) + 1
            plan = Plan(
                id=plan_id,
                project_id=state.project.id,
                version=plan_version,
                status=PlanStatus.ACTIVE,
                requirement_ids=(
                    proposal.requirements_considered
                    + tuple(item.id for item in proposal.requirements)
                ),
                milestone_ids=tuple(item.id for item in proposal.milestones),
                created_at=operation_time,
            )
        except (KeyError, ValueError) as error:
            raise PlanMaterializationError(
                "Validated proposal data could not form domain entities",
                code=PlanningValidationCode.DOMAIN_CONSTRUCTION,
                field_path="proposal",
            ) from error
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
            requirements=state.requirements + requirements,
            plans=state.plans + (plan,),
            milestones=state.milestones + milestones,
            tasks=state.tasks + tasks,
        )
        if not state.revisions:
            materialized = begin_revision(
                materialized,
                revision_number=1,
                plan_id=plan.id,
                plan_version=plan.version,
                change_request_id=None,
                base_revision=None,
                started_at=operation_time,
                baseline_head=None,
            )
        return materialized


__all__ = ["PlanMaterializer"]
