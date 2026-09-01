"""Deterministic validation of untrusted Supervisor change proposals."""

from code_mule.domain.enums import RequirementStatus, TaskStatus
from code_mule.domain.models import ChangeRequest, Milestone, Task
from code_mule.scheduler import SchedulerError
from code_mule.scheduler.selection import resolve_active_plan_graph
from code_mule.state.models import ProjectState
from code_mule.supervisor.contracts import ImpactAnalysisResult

from .errors import (
    ConflictingTaskChange,
    InvalidReplanDependency,
    InvalidReplanProposal,
    ReplanDependencyCycle,
    ReplanIdCollision,
    UnknownReplanReference,
)


class ChangeReplanValidator:
    """Validate identity, lifecycle changes, traceability, and replacement graph."""

    def validate(
        self,
        state: ProjectState,
        change_request: ChangeRequest,
        proposal: ImpactAnalysisResult,
    ) -> None:
        if proposal.change_request_id != change_request.id:
            raise UnknownReplanReference(
                "proposal targets a different ChangeRequest ID"
            )
        try:
            graph = resolve_active_plan_graph(state)
        except SchedulerError as error:
            raise InvalidReplanProposal(
                "current active Plan graph is invalid"
            ) from error

        existing_requirements = {item.id: item for item in state.requirements}
        active_requirement_ids = tuple(graph.plan.requirement_ids)
        active_requirements = set(active_requirement_ids)
        existing_tasks = {item.id: item for item in graph.tasks}
        active_task_ids = tuple(item.id for item in graph.tasks)
        active_tasks = set(active_task_ids)
        existing_milestones = {item.id: item for item in state.milestones}

        self._known(
            proposal.affected_requirement_ids,
            active_requirements,
            "affected Requirement",
        )
        self._known(proposal.affected_task_ids, active_tasks, "affected Task")
        classified = (
            proposal.affected_completed_tasks
            + proposal.affected_in_progress_tasks
            + proposal.affected_pending_tasks
        )
        self._unique(classified, "affected Task classification")
        self._known(classified, set(proposal.affected_task_ids), "classified Task")
        if set(classified) != set(proposal.affected_task_ids):
            raise InvalidReplanProposal(
                "every affected Task must have exactly one status classification"
            )
        self._validate_classifications(existing_tasks, proposal)

        added_requirements = tuple(item.id for item in proposal.requirements_to_add)
        updated_requirements = tuple(
            item.requirement.id for item in proposal.requirements_to_update
        )
        superseded_requirements = tuple(
            item.supersedes_id for item in proposal.requirements_to_update
        )
        added_tasks = tuple(item.id for item in proposal.tasks_to_add)
        milestone_ids = tuple(item.id for item in proposal.milestones)
        all_new_ids = (
            added_requirements
            + updated_requirements
            + added_tasks
            + milestone_ids
        )
        self._unique(all_new_ids, "new entity")
        occupied_ids = {
            state.project.id,
            *(item.id for item in state.requirements),
            *(item.id for item in state.plans),
            *(item.id for item in state.milestones),
            *(item.id for item in state.tasks),
            *(item.id for item in state.change_requests),
        }
        for entity_id in all_new_ids:
            if entity_id in occupied_ids:
                raise ReplanIdCollision(
                    f"new entity ID collides with history: {entity_id}"
                )

        self._unique(superseded_requirements, "superseded Requirement")
        self._known(
            superseded_requirements,
            active_requirements,
            "superseded Requirement",
        )
        self._known(
            superseded_requirements,
            set(proposal.affected_requirement_ids),
            "Requirement update target",
        )
        for requirement_id in superseded_requirements:
            requirement = existing_requirements[requirement_id]
            if requirement.status is not RequirementStatus.ACTIVE:
                raise InvalidReplanProposal(
                    f"only ACTIVE Requirements may be superseded: {requirement_id}"
                )

        self._validate_task_changes(existing_tasks, proposal)
        controlled_tasks = (
            proposal.tasks_to_reopen
            + proposal.tasks_to_cancel
            + tuple(item.task_id for item in proposal.dependency_changes)
            + tuple(
                item.task_id for item in proposal.task_requirement_updates
            )
        )
        self._known(
            controlled_tasks,
            set(proposal.affected_task_ids),
            "Task change target",
        )
        cancelled = set(proposal.tasks_to_cancel)
        retained_task_ids = tuple(
            task_id for task_id in active_task_ids if task_id not in cancelled
        )
        replacement_task_ids = retained_task_ids + added_tasks
        replacement_tasks = set(replacement_task_ids)
        if not replacement_task_ids:
            raise InvalidReplanProposal("replacement Plan must contain a Task")

        replacement_requirement_ids = tuple(
            requirement_id
            for requirement_id in active_requirement_ids
            if requirement_id not in set(superseded_requirements)
        ) + added_requirements + updated_requirements
        replacement_requirements = set(replacement_requirement_ids)
        if not replacement_requirement_ids:
            raise InvalidReplanProposal(
                "replacement Plan must contain a Requirement"
            )

        dependency_changes = {
            item.task_id: item.dependencies for item in proposal.dependency_changes
        }
        self._unique(
            tuple(item.task_id for item in proposal.dependency_changes),
            "dependency-change Task",
        )
        self._known(
            tuple(dependency_changes),
            set(retained_task_ids),
            "dependency-change Task",
        )
        task_requirement_updates = {
            item.task_id: item.requirement_ids
            for item in proposal.task_requirement_updates
        }
        self._unique(
            tuple(item.task_id for item in proposal.task_requirement_updates),
            "task-requirement-update Task",
        )
        self._known(
            tuple(task_requirement_updates),
            set(retained_task_ids),
            "task-requirement-update Task",
        )
        new_tasks = {item.id: item for item in proposal.tasks_to_add}
        dependencies: dict[str, tuple[str, ...]] = {}
        requirement_references: dict[str, tuple[str, ...]] = {}
        for task_id in replacement_task_ids:
            if task_id in new_tasks:
                task_dependencies = new_tasks[task_id].dependencies
                requirement_ids = new_tasks[task_id].requirement_ids
            else:
                task = existing_tasks[task_id]
                if task.status in {TaskStatus.BLOCKED, TaskStatus.IN_PROGRESS}:
                    raise InvalidReplanProposal(
                        f"replacement Plan contains unsafe Task status: {task_id}"
                    )
                task_dependencies = dependency_changes.get(
                    task_id, task.dependencies
                )
                requirement_ids = task_requirement_updates.get(
                    task_id, task.requirement_ids
                )
            self._unique(task_dependencies, f"Task {task_id} dependency")
            for dependency_id in task_dependencies:
                if dependency_id == task_id:
                    raise InvalidReplanDependency(
                        f"Task cannot depend on itself: {task_id}"
                    )
                if dependency_id not in replacement_tasks:
                    raise InvalidReplanDependency(
                        f"Task {task_id} has unavailable dependency: {dependency_id}"
                    )
            if not requirement_ids:
                raise InvalidReplanProposal(
                    f"Task has no Requirement traceability: {task_id}"
                )
            self._unique(
                requirement_ids, f"Task {task_id} Requirement traceability"
            )
            for requirement_id in requirement_ids:
                if requirement_id not in replacement_requirements:
                    raise UnknownReplanReference(
                        f"Task {task_id} references unavailable Requirement: "
                        f"{requirement_id}"
                    )
            dependencies[task_id] = task_dependencies
            requirement_references[task_id] = requirement_ids

        self._acyclic(replacement_task_ids, dependencies)
        self._unique(proposal.milestone_ids_reused, "reused Milestone")
        self._known(
            proposal.milestone_ids_reused,
            set(existing_milestones),
            "reused Milestone",
        )
        self._validate_milestones(
            proposal, replacement_tasks, existing_milestones
        )
        covered = {
            requirement_id
            for requirement_ids in requirement_references.values()
            for requirement_id in requirement_ids
        }
        uncovered = tuple(
            requirement_id
            for requirement_id in replacement_requirement_ids
            if requirement_id not in covered
        )
        if uncovered:
            raise InvalidReplanProposal(
                "active Requirements without Task coverage: " + ", ".join(uncovered)
            )

    def _validate_task_changes(
        self,
        existing_tasks: dict[str, Task],
        proposal: ImpactAnalysisResult,
    ) -> None:
        reopen = proposal.tasks_to_reopen
        cancel = proposal.tasks_to_cancel
        self._unique(reopen, "reopened Task")
        self._unique(cancel, "cancelled Task")
        self._known(reopen, set(existing_tasks), "reopened Task")
        self._known(cancel, set(existing_tasks), "cancelled Task")
        conflict = set(reopen) & set(cancel)
        if conflict:
            raise ConflictingTaskChange(
                "Task cannot be reopened and cancelled: "
                + ", ".join(sorted(conflict))
            )
        for task_id in reopen:
            if existing_tasks[task_id].status is not TaskStatus.COMPLETED:
                raise ConflictingTaskChange(
                    f"only COMPLETED Tasks may be reopened: {task_id}"
                )
        for task_id in cancel:
            if existing_tasks[task_id].status is TaskStatus.COMPLETED:
                raise ConflictingTaskChange(
                    f"COMPLETED Task cannot be cancelled: {task_id}"
                )

    @staticmethod
    def _validate_classifications(
        tasks: dict[str, Task], proposal: ImpactAnalysisResult
    ) -> None:
        expected = (
            (proposal.affected_completed_tasks, TaskStatus.COMPLETED),
            (proposal.affected_in_progress_tasks, TaskStatus.IN_PROGRESS),
        )
        for task_ids, status in expected:
            for task_id in task_ids:
                if tasks[task_id].status is not status:
                    raise InvalidReplanProposal(
                        f"affected Task status classification is incorrect: {task_id}"
                    )
        for task_id in proposal.affected_pending_tasks:
            if tasks[task_id].status not in {
                TaskStatus.PENDING,
                TaskStatus.REOPENED,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
            }:
                raise InvalidReplanProposal(
                    f"affected pending Task classification is incorrect: {task_id}"
                )

    def _validate_milestones(
        self,
        proposal: ImpactAnalysisResult,
        replacement_tasks: set[str],
        existing_milestones: dict[str, Milestone],
    ) -> None:
        if not proposal.milestone_ids_reused and not proposal.milestones:
            raise InvalidReplanProposal(
                "replacement Plan must contain a Milestone"
            )
        membership: dict[str, int] = {task_id: 0 for task_id in replacement_tasks}
        for milestone_id in proposal.milestone_ids_reused:
            milestone = existing_milestones[milestone_id]
            if not milestone.task_ids:
                raise InvalidReplanProposal(
                    f"reused Milestone must contain a Task: {milestone_id}"
                )
            self._unique(
                milestone.task_ids, f"reused Milestone {milestone_id} Task"
            )
            for task_id in milestone.task_ids:
                if task_id not in replacement_tasks:
                    raise UnknownReplanReference(
                        f"reused Milestone {milestone_id} references unavailable "
                        f"Task: {task_id}"
                    )
                membership[task_id] += 1
        for milestone in proposal.milestones:
            if not milestone.task_ids:
                raise InvalidReplanProposal(
                    f"Milestone must contain a Task: {milestone.id}"
                )
            self._unique(milestone.task_ids, f"Milestone {milestone.id} Task")
            for task_id in milestone.task_ids:
                if task_id not in replacement_tasks:
                    raise UnknownReplanReference(
                        f"Milestone {milestone.id} references unavailable Task: "
                        f"{task_id}"
                    )
                membership[task_id] += 1
        invalid = tuple(
            task_id for task_id, count in membership.items() if count != 1
        )
        if invalid:
            raise InvalidReplanProposal(
                "every active Task must belong to exactly one Milestone: "
                + ", ".join(invalid)
            )

    @staticmethod
    def _known(
        values: tuple[str, ...], allowed: set[str], context: str
    ) -> None:
        for value in values:
            if value not in allowed:
                raise UnknownReplanReference(f"unknown {context}: {value}")

    @staticmethod
    def _unique(values: tuple[str, ...], context: str) -> None:
        if len(values) != len(set(values)):
            raise InvalidReplanProposal(f"duplicate {context} ID/reference")

    @staticmethod
    def _acyclic(
        task_ids: tuple[str, ...], dependencies: dict[str, tuple[str, ...]]
    ) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ReplanDependencyCycle(
                    f"replacement Task dependency cycle includes: {task_id}"
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


__all__ = ["ChangeReplanValidator"]
