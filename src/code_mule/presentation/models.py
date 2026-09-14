"""Pure view models derived from persisted domain state."""

from dataclasses import dataclass

from code_mule.domain.enums import HumanActionStatus
from code_mule.domain.models import HumanAction
from code_mule.state.models import ProjectState
from code_mule.revision import latest_revision

from .labels import humanize_identifier, status_label


@dataclass(frozen=True)
class ProjectView:
    name: str
    status: str
    revision_number: int | None
    plan_version: int | None
    completed_tasks: int
    total_tasks: int
    current_task: str | None
    boss_action: str | None
    project_id: str
    raw_status: str
    active_plan_id: str | None
    current_task_id: str | None
    latest_task_commit: str | None
    reused_task_count: int = 0


@dataclass(frozen=True)
class HumanActionView:
    category: str
    task: str | None
    summary: str
    request: str
    risk: str
    created: str
    action_id: str
    raw_status: str
    project_id: str
    task_id: str | None
    approvable: bool
    question: str | None
    choices: tuple[str, ...]
    worker_request_method: str | None
    worker_request_id: str | None
    capability: str | None = None
    application: str | None = None
    approval_scopes: tuple[str, ...] = ()
    native_request_active: bool | None = None
    approval_type: str | None = None
    continuation: str | None = None


def project_view(state: ProjectState) -> ProjectView:
    active_plan = next(
        (plan for plan in state.plans if plan.id == state.project.active_plan_id),
        None,
    )
    latest = latest_revision(state)
    milestone_ids = set(active_plan.milestone_ids if active_plan else ())
    task_ids = {
        task_id
        for milestone in state.milestones
        if milestone.id in milestone_ids
        for task_id in milestone.task_ids
    }
    active_tasks = tuple(task for task in state.tasks if task.id in task_ids)
    completed = sum(task.status.value == "completed" for task in active_tasks)
    current = next(
        (task for task in state.tasks if task.id == state.project.current_task_id),
        None,
    )
    pending = tuple(
        action
        for action in state.human_actions
        if action.status is HumanActionStatus.PENDING
    )
    boss_action = (
        humanize_identifier(pending[0].category.value)
        if len(pending) == 1
        else ("Review required" if len(pending) > 1 else None)
    )
    return ProjectView(
        name=state.project.name,
        status=status_label(state.project.status),
        revision_number=None if latest is None else latest.revision_number,
        reused_task_count=(
            0 if active_plan is None else len(active_plan.reused_task_ids)
        ),
        plan_version=None if active_plan is None else active_plan.version,
        completed_tasks=completed,
        total_tasks=len(active_tasks),
        current_task=(
            None if current is None else f"{current.id} · {current.title}"
        ),
        boss_action=boss_action,
        project_id=state.project.id,
        raw_status=state.project.status.value,
        active_plan_id=state.project.active_plan_id,
        current_task_id=state.project.current_task_id,
        latest_task_commit=(
            None
            if not state.git_commit_results
            else state.git_commit_results[-1].commit_sha
        ),
    )


def human_action_view(action: HumanAction) -> HumanActionView:
    worker_input = action.worker_input
    capability = action.capability_approval
    return HumanActionView(
        category=humanize_identifier(action.category.value),
        task=action.task_id,
        summary=action.summary,
        request=action.requested_action,
        risk=action.risk,
        created=action.created_at.isoformat(),
        action_id=action.id,
        raw_status=action.status.value,
        project_id=action.project_id,
        task_id=action.task_id,
        approvable=(
            action.category.value in {"worker_approval", "external_side_effect"}
            and not (capability is not None and not capability.native_request_active)
        ),
        question=None if worker_input is None else worker_input.question,
        choices=() if worker_input is None else worker_input.choices,
        worker_request_method=(
            capability.request_method
            if capability is not None
            else None if worker_input is None else worker_input.request_method
        ),
        worker_request_id=(
            capability.request_id
            if capability is not None
            else None if worker_input is None else worker_input.request_id
        ),
        capability=None if capability is None else capability.capability,
        application=None if capability is None else capability.application,
        approval_scopes=(
            ()
            if capability is None
            else tuple(scope.value for scope in capability.approval_scopes)
        ),
        native_request_active=(
            None if capability is None else capability.native_request_active
        ),
        approval_type=(
            None
            if action.category.value not in {"worker_approval", "external_side_effect"}
            else "Product / report approval"
            if capability is None
            else "Native capability approval"
            if capability.request_method == "mcpServer/elicitation/request"
            else "Native sandbox approval"
        ),
        continuation=(
            None
            if action.category.value not in {"worker_approval", "external_side_effect"}
            else "Fresh Worker continuation is not automatic"
            if capability is None
            else "Same session required; original request expired"
            if not capability.native_request_active
            else "Same live session required"
        ),
    )


__all__ = [
    "HumanActionView",
    "ProjectView",
    "human_action_view",
    "project_view",
]
