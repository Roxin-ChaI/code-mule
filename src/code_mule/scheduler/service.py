"""Pure deterministic scheduler facade."""

from code_mule.domain.models import Task
from code_mule.state.models import ProjectState

from .selection import (
    is_active_plan_complete,
    resolve_active_plan_graph,
    select_next_task,
)


class TaskScheduler:
    def validate(self, state: ProjectState) -> None:
        resolve_active_plan_graph(state)

    def select_next(self, state: ProjectState) -> Task | None:
        return select_next_task(state)

    def is_plan_complete(self, state: ProjectState) -> bool:
        return is_active_plan_complete(state)


__all__ = ["TaskScheduler"]
