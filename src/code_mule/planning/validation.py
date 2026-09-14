"""Deterministic validation for untrusted Supervisor planning proposals."""

from code_mule.domain.enums import RequirementStatus
from code_mule.state.models import ProjectState
from code_mule.supervisor.contracts import PlanProposal

from .errors import (
    DuplicateProposalId,
    InvalidPlanProposal,
    InvalidProposalDependency,
    PlanningValidationCode,
    ProposalDependencyCycle,
    UnknownProposalReference,
)


class PlanProposalValidator:
    """Validate proposal identity, graph, membership, and traceability."""

    def validate(self, state: ProjectState, proposal: PlanProposal) -> None:
        requirement_ids = tuple(item.id for item in proposal.requirements)
        milestone_ids = tuple(item.id for item in proposal.milestones)
        task_ids = tuple(item.id for item in proposal.tasks)

        self._unique(requirement_ids, "requirements")
        self._unique(milestone_ids, "milestones")
        self._unique(task_ids, "tasks")
        all_proposed = requirement_ids + milestone_ids + task_ids
        self._unique(all_proposed, "proposal")
        self._reject_existing_collisions(state, all_proposed)

        existing_requirements = {item.id: item for item in state.requirements}
        self._unique(proposal.requirements_considered, "requirements_considered")
        for index, requirement_id in enumerate(proposal.requirements_considered):
            requirement = existing_requirements.get(requirement_id)
            if requirement is None:
                raise UnknownProposalReference(
                    "A considered Requirement ID is not present in ProjectState",
                    code=PlanningValidationCode.UNKNOWN_REQUIREMENT_REFERENCE,
                    field_path=f"requirements_considered[{index}]",
                )
            if (
                requirement.project_id != state.project.id
                or requirement.status is not RequirementStatus.ACTIVE
            ):
                raise InvalidPlanProposal(
                    "A considered Requirement is not active for this Project",
                    code=PlanningValidationCode.INACTIVE_REQUIREMENT_REFERENCE,
                    field_path=f"requirements_considered[{index}]",
                )

        plan_requirement_ids = proposal.requirements_considered + requirement_ids
        if not plan_requirement_ids:
            raise InvalidPlanProposal(
                "A Plan must contain at least one Requirement",
                code=PlanningValidationCode.EMPTY_REQUIREMENTS,
                field_path="requirements",
            )
        if not proposal.milestones:
            raise InvalidPlanProposal(
                "A Plan must contain at least one Milestone",
                code=PlanningValidationCode.EMPTY_MILESTONES,
                field_path="milestones",
            )
        if not proposal.tasks:
            raise InvalidPlanProposal(
                "A Plan must contain at least one Task",
                code=PlanningValidationCode.EMPTY_TASKS,
                field_path="tasks",
            )

        proposed_task_ids = set(task_ids)
        membership: dict[str, int] = {task_id: 0 for task_id in task_ids}
        for milestone_index, milestone in enumerate(proposal.milestones):
            if not milestone.task_ids:
                raise InvalidPlanProposal(
                    "A Milestone must contain at least one Task",
                    code=PlanningValidationCode.EMPTY_MILESTONE,
                    field_path=f"milestones[{milestone_index}].task_ids",
                )
            self._unique(
                milestone.task_ids, f"milestones[{milestone_index}].task_ids"
            )
            for task_index, task_id in enumerate(milestone.task_ids):
                if task_id not in proposed_task_ids:
                    raise UnknownProposalReference(
                        "A Milestone references a Task absent from the proposal",
                        code=PlanningValidationCode.UNKNOWN_TASK_REFERENCE,
                        field_path=(
                            f"milestones[{milestone_index}].task_ids[{task_index}]"
                        ),
                    )
                membership[task_id] += 1
        for task_index, task_id in enumerate(task_ids):
            count = membership[task_id]
            if count != 1:
                raise InvalidPlanProposal(
                    "Each Task must belong to exactly one Milestone",
                    code=PlanningValidationCode.TASK_MILESTONE_MEMBERSHIP,
                    field_path=f"tasks[{task_index}].id",
                )

        allowed_requirements = set(plan_requirement_ids)
        covered_requirements: set[str] = set()
        dependencies: dict[str, tuple[str, ...]] = {}
        for task_index, task in enumerate(proposal.tasks):
            if not task.acceptance_criteria:
                raise InvalidPlanProposal(
                    "A Task must have acceptance criteria",
                    code=PlanningValidationCode.MISSING_ACCEPTANCE_CRITERIA,
                    field_path=f"tasks[{task_index}].acceptance_criteria",
                )
            if not task.requirement_ids:
                raise InvalidPlanProposal(
                    "A Task must reference at least one Requirement",
                    code=PlanningValidationCode.MISSING_REQUIREMENT_REFERENCE,
                    field_path=f"tasks[{task_index}].requirement_ids",
                )
            self._unique(
                task.requirement_ids, f"tasks[{task_index}].requirement_ids"
            )
            for requirement_index, requirement_id in enumerate(task.requirement_ids):
                if requirement_id not in allowed_requirements:
                    raise UnknownProposalReference(
                        "A Task references a Requirement absent from the Plan",
                        code=PlanningValidationCode.UNKNOWN_REQUIREMENT_REFERENCE,
                        field_path=(
                            f"tasks[{task_index}].requirement_ids[{requirement_index}]"
                        ),
                    )
                covered_requirements.add(requirement_id)

            self._unique(task.dependencies, f"tasks[{task_index}].dependencies")
            if task.id in task.dependencies:
                raise InvalidProposalDependency(
                    "A Task cannot depend on itself",
                    code=PlanningValidationCode.INVALID_DEPENDENCY,
                    field_path=f"tasks[{task_index}].dependencies",
                )
            for dependency_index, dependency_id in enumerate(task.dependencies):
                if dependency_id not in proposed_task_ids:
                    raise InvalidProposalDependency(
                        "A Task dependency is absent from the proposal",
                        code=PlanningValidationCode.INVALID_DEPENDENCY,
                        field_path=(
                            f"tasks[{task_index}].dependencies[{dependency_index}]"
                        ),
                    )
            dependencies[task.id] = task.dependencies

        uncovered = tuple(
            requirement_id
            for requirement_id in plan_requirement_ids
            if requirement_id not in covered_requirements
        )
        if uncovered:
            raise InvalidPlanProposal(
                "Every Plan Requirement must be covered by at least one Task",
                code=PlanningValidationCode.UNCOVERED_REQUIREMENT,
                field_path="requirements",
            )
        self._reject_cycles(task_ids, dependencies)

    @staticmethod
    def _unique(values: tuple[str, ...], field_path: str) -> None:
        seen: set[str] = set()
        for index, value in enumerate(values):
            if value in seen:
                raise DuplicateProposalId(
                    "Proposal identifiers and references must be unique",
                    code=PlanningValidationCode.DUPLICATE_ID,
                    field_path=f"{field_path}[{index}]",
                )
            seen.add(value)

    @staticmethod
    def _reject_existing_collisions(
        state: ProjectState, proposed_ids: tuple[str, ...]
    ) -> None:
        existing_ids = {
            *(item.id for item in state.requirements),
            *(item.id for item in state.plans),
            *(item.id for item in state.milestones),
            *(item.id for item in state.tasks),
            *(item.id for item in state.change_requests),
        }
        for index, proposed_id in enumerate(proposed_ids):
            if proposed_id in existing_ids:
                raise InvalidPlanProposal(
                    "A proposed entity ID collides with persisted ProjectState",
                    code=PlanningValidationCode.EXISTING_ID_COLLISION,
                    field_path=f"proposal[{index}]",
                )

    @staticmethod
    def _reject_cycles(
        task_ids: tuple[str, ...], dependencies: dict[str, tuple[str, ...]]
    ) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        task_indexes = {task_id: index for index, task_id in enumerate(task_ids)}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ProposalDependencyCycle(
                    "The proposed Task dependency graph contains a cycle",
                    code=PlanningValidationCode.DEPENDENCY_CYCLE,
                    field_path=f"tasks[{task_indexes[task_id]}].dependencies",
                )
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in dependencies[task_id]:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)


__all__ = ["PlanProposalValidator"]
