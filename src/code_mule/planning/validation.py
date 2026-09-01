"""Deterministic validation for untrusted Supervisor planning proposals."""

from code_mule.domain.enums import RequirementStatus
from code_mule.state.models import ProjectState
from code_mule.supervisor.contracts import PlanProposal

from .errors import (
    DuplicateProposalId,
    InvalidPlanProposal,
    InvalidProposalDependency,
    ProposalDependencyCycle,
    UnknownProposalReference,
)


class PlanProposalValidator:
    """Validate proposal identity, graph, membership, and traceability."""

    def validate(self, state: ProjectState, proposal: PlanProposal) -> None:
        requirement_ids = tuple(item.id for item in proposal.requirements)
        milestone_ids = tuple(item.id for item in proposal.milestones)
        task_ids = tuple(item.id for item in proposal.tasks)

        self._unique(requirement_ids, "Requirement")
        self._unique(milestone_ids, "Milestone")
        self._unique(task_ids, "Task")
        all_proposed = requirement_ids + milestone_ids + task_ids
        self._unique(all_proposed, "proposal entity")
        self._reject_existing_collisions(state, all_proposed)

        existing_requirements = {item.id: item for item in state.requirements}
        self._unique(proposal.requirements_considered, "considered Requirement")
        for requirement_id in proposal.requirements_considered:
            requirement = existing_requirements.get(requirement_id)
            if requirement is None:
                raise UnknownProposalReference(
                    f"unknown considered Requirement: {requirement_id}"
                )
            if (
                requirement.project_id != state.project.id
                or requirement.status is not RequirementStatus.ACTIVE
            ):
                raise InvalidPlanProposal(
                    f"considered Requirement is not active for this Project: {requirement_id}"
                )

        plan_requirement_ids = proposal.requirements_considered + requirement_ids
        if not plan_requirement_ids:
            raise InvalidPlanProposal("a Plan must contain at least one Requirement")
        if not proposal.milestones:
            raise InvalidPlanProposal("a Plan must contain at least one Milestone")
        if not proposal.tasks:
            raise InvalidPlanProposal("a Plan must contain at least one Task")

        proposed_task_ids = set(task_ids)
        membership: dict[str, int] = {task_id: 0 for task_id in task_ids}
        for milestone in proposal.milestones:
            if not milestone.task_ids:
                raise InvalidPlanProposal(
                    f"Milestone must contain at least one Task: {milestone.id}"
                )
            self._unique(milestone.task_ids, f"Milestone {milestone.id} Task reference")
            for task_id in milestone.task_ids:
                if task_id not in proposed_task_ids:
                    raise UnknownProposalReference(
                        f"Milestone {milestone.id} references unknown Task: {task_id}"
                    )
                membership[task_id] += 1
        for task_id, count in membership.items():
            if count != 1:
                raise InvalidPlanProposal(
                    f"Task must belong to exactly one Milestone: {task_id}"
                )

        allowed_requirements = set(plan_requirement_ids)
        covered_requirements: set[str] = set()
        dependencies: dict[str, tuple[str, ...]] = {}
        for task in proposal.tasks:
            if not task.acceptance_criteria:
                raise InvalidPlanProposal(
                    f"Task must have acceptance criteria: {task.id}"
                )
            if not task.requirement_ids:
                raise InvalidPlanProposal(
                    f"Task must reference at least one Requirement: {task.id}"
                )
            self._unique(task.requirement_ids, f"Task {task.id} Requirement reference")
            for requirement_id in task.requirement_ids:
                if requirement_id not in allowed_requirements:
                    raise UnknownProposalReference(
                        f"Task {task.id} references unknown Requirement: {requirement_id}"
                    )
                covered_requirements.add(requirement_id)

            self._unique(task.dependencies, f"Task {task.id} dependency")
            if task.id in task.dependencies:
                raise InvalidProposalDependency(
                    f"Task cannot depend on itself: {task.id}"
                )
            for dependency_id in task.dependencies:
                if dependency_id not in proposed_task_ids:
                    raise InvalidProposalDependency(
                        f"Task {task.id} has unknown dependency: {dependency_id}"
                    )
            dependencies[task.id] = task.dependencies

        uncovered = tuple(
            requirement_id
            for requirement_id in plan_requirement_ids
            if requirement_id not in covered_requirements
        )
        if uncovered:
            raise InvalidPlanProposal(
                "Plan Requirements without a Task: " + ", ".join(uncovered)
            )
        self._reject_cycles(task_ids, dependencies)

    @staticmethod
    def _unique(values: tuple[str, ...], context: str) -> None:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                raise DuplicateProposalId(f"duplicate {context} ID/reference: {value}")
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
        for proposed_id in proposed_ids:
            if proposed_id in existing_ids:
                raise InvalidPlanProposal(
                    f"proposal ID collides with existing state: {proposed_id}"
                )

    @staticmethod
    def _reject_cycles(
        task_ids: tuple[str, ...], dependencies: dict[str, tuple[str, ...]]
    ) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ProposalDependencyCycle(
                    f"proposed Task dependency cycle includes: {task_id}"
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
