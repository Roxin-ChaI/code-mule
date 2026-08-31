"""Pure active-Plan graph resolution and stable Task selection."""

from dataclasses import dataclass

from code_mule.domain.enums import PlanStatus, TaskStatus
from code_mule.domain.models import Milestone, Plan, Task
from code_mule.state.models import ProjectState

from .errors import (
    DependencyCycleDetected,
    NoActivePlan,
    PlanStateInvalid,
    TaskGraphInvalid,
    UnknownTaskDependency,
)


@dataclass(frozen=True)
class ActivePlanGraph:
    plan: Plan
    milestones: tuple[Milestone, ...]
    tasks: tuple[Task, ...]


def resolve_active_plan_graph(state: ProjectState) -> ActivePlanGraph:
    """Resolve one unambiguous active graph in declared execution order."""

    active_plan_id = state.project.active_plan_id
    if active_plan_id is None:
        raise NoActivePlan("project.active_plan_id is not set")
    plans = tuple(plan for plan in state.plans if plan.id == active_plan_id)
    if len(plans) != 1:
        raise PlanStateInvalid(
            f"active Plan {active_plan_id!r} must exist exactly once"
        )
    plan = plans[0]
    if plan.project_id != state.project.id:
        raise PlanStateInvalid("active Plan belongs to a different project")
    if plan.status is not PlanStatus.ACTIVE:
        raise PlanStateInvalid("project.active_plan_id must reference an ACTIVE Plan")
    if len(plan.milestone_ids) != len(dict.fromkeys(plan.milestone_ids)):
        raise PlanStateInvalid("active Plan contains duplicate Milestone IDs")

    milestones: list[Milestone] = []
    tasks: list[Task] = []
    seen_task_ids: dict[str, None] = {}
    for milestone_id in plan.milestone_ids:
        matches = tuple(
            milestone for milestone in state.milestones if milestone.id == milestone_id
        )
        if len(matches) != 1:
            raise PlanStateInvalid(
                f"Milestone {milestone_id!r} must exist exactly once"
            )
        milestone = matches[0]
        if milestone.plan_id != plan.id:
            raise PlanStateInvalid(
                f"Milestone {milestone.id!r} belongs to a different Plan"
            )
        if len(milestone.task_ids) != len(dict.fromkeys(milestone.task_ids)):
            raise TaskGraphInvalid(
                f"Milestone {milestone.id!r} contains duplicate Task IDs"
            )
        milestones.append(milestone)
        for task_id in milestone.task_ids:
            if task_id in seen_task_ids:
                raise TaskGraphInvalid(
                    f"Task {task_id!r} appears more than once in active Plan"
                )
            matches = tuple(task for task in state.tasks if task.id == task_id)
            if len(matches) != 1:
                raise TaskGraphInvalid(
                    f"Task {task_id!r} must exist exactly once"
                )
            task = matches[0]
            if task.milestone_id != milestone.id:
                raise TaskGraphInvalid(
                    f"Task {task.id!r} belongs to a different Milestone"
                )
            seen_task_ids[task.id] = None
            tasks.append(task)

    if not tasks:
        raise PlanStateInvalid("active Plan must contain at least one Task")

    task_ids = dict.fromkeys(task.id for task in tasks)
    for task in tasks:
        for dependency_id in task.dependencies:
            if dependency_id not in task_ids:
                raise UnknownTaskDependency(
                    f"Task {task.id!r} has unknown dependency {dependency_id!r}"
                )
    _validate_acyclic(tuple(tasks))
    return ActivePlanGraph(plan, tuple(milestones), tuple(tasks))


def select_next_task(state: ProjectState) -> Task | None:
    """Return the first Ready Task in active Plan declaration order."""

    graph = resolve_active_plan_graph(state)
    tasks_by_id = {task.id: task for task in graph.tasks}
    for task in graph.tasks:
        if task.status not in {TaskStatus.PENDING, TaskStatus.REOPENED}:
            continue
        if all(
            tasks_by_id[dependency_id].status is TaskStatus.COMPLETED
            for dependency_id in task.dependencies
        ):
            return task
    return None


def is_active_plan_complete(state: ProjectState) -> bool:
    graph = resolve_active_plan_graph(state)
    return all(
        task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
        for task in graph.tasks
    )


def _validate_acyclic(tasks: tuple[Task, ...]) -> None:
    tasks_by_id = {task.id: task for task in tasks}
    states: dict[str, str] = {}

    def visit(task_id: str) -> None:
        state = states.get(task_id)
        if state == "visiting":
            raise DependencyCycleDetected(
                f"dependency cycle detected at Task {task_id!r}"
            )
        if state == "visited":
            return
        states[task_id] = "visiting"
        for dependency_id in tasks_by_id[task_id].dependencies:
            visit(dependency_id)
        states[task_id] = "visited"

    for task in tasks:
        visit(task.id)


__all__ = [
    "ActivePlanGraph",
    "is_active_plan_complete",
    "resolve_active_plan_graph",
    "select_next_task",
]
