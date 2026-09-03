"""Line-oriented Boss CLI rendering from immutable view models."""

from code_mule.domain.models import HumanAction
from code_mule.state.models import ProjectState

from .models import human_action_view, project_view


def render_project(
    state: ProjectState,
    *,
    verbose: bool = False,
    heading: str = "PROJECT",
    execution_stop_reason: str | None = None,
) -> tuple[str, ...]:
    view = project_view(state)
    lines = (
        heading,
        view.name,
        "",
        f"Status      {view.status}",
        f"Plan        {'—' if view.plan_version is None else f'v{view.plan_version}'}",
        f"Progress    {view.completed_tasks} / {view.total_tasks}",
        f"Current     {view.current_task or 'None'}",
        f"Boss action {view.boss_action or 'None'}",
    )
    if verbose:
        lines += (
            "",
            "INTERNAL",
            f"project_id: {view.project_id}",
            f"project_status: {view.raw_status}",
            f"active_plan_id: {view.active_plan_id or '-'}",
            f"active_plan_version: {view.plan_version or '-'}",
            f"current_task_id: {view.current_task_id or '-'}",
            f"execution_stop_reason: {execution_stop_reason or '-'}",
            f"latest_task_commit: {view.latest_task_commit or '-'}",
        )
    return lines


def render_change_requested(
    state: ProjectState, request: str, *, verbose: bool = False
) -> tuple[str, ...]:
    view = project_view(state)
    lines = (
        "CHANGE REQUESTED",
        "",
        f'"{request}"',
        "",
        "Current task will finish safely before replanning.",
        f"Plan        {'—' if view.plan_version is None else f'v{view.plan_version}'}",
        "Next        Impact analysis",
        "",
        "Run:",
        "  code-mule change --apply",
    )
    if verbose:
        lines += (
            "",
            f"project_id: {view.project_id}",
            f"project_status: {view.raw_status}",
            f"active_plan_id: {view.active_plan_id or '-'}",
        )
    return lines


def render_change_applied(
    before: ProjectState,
    after: ProjectState,
    *,
    verbose: bool = False,
    execution_stop_reason: str | None = None,
) -> tuple[str, ...]:
    old = project_view(before)
    new = project_view(after)
    lines = (
        "REPLANNING",
        "✓ Impact analysis completed",
        "✓ Existing work preserved",
        f"✓ Plan v{old.plan_version or '—'} superseded",
        f"✓ Plan v{new.plan_version or '—'} activated",
        "",
        "Execution resumed.",
    ) + render_project(
        after,
        verbose=verbose,
        heading="PROJECT",
        execution_stop_reason=execution_stop_reason,
    )
    return lines


def render_human_action(
    action: HumanAction, *, verbose: bool = False
) -> tuple[str, ...]:
    view = human_action_view(action)
    lines = (
        "ACTION REQUIRED",
        "────────────────────────",
        "",
        f"Category    {view.category}",
        f"Task        {view.task or 'None'}",
        f"Request     {view.request}",
        f"Risk        {view.risk}",
        "",
        "No action has been executed.",
    )
    if view.approvable:
        lines += (
            "",
            "Approve:",
            f"  code-mule approve {view.action_id}",
            "",
            "Reject:",
            f"  code-mule reject {view.action_id}",
        )
    else:
        lines += (
            "",
            "Resolve:",
            f"  code-mule resolve {view.action_id} --strategy <strategy>",
        )
    if verbose:
        lines += (
            "",
            "INTERNAL",
            f"action_id: {view.action_id}",
            f"project_id: {view.project_id}",
            f"task_id: {view.task_id or '-'}",
            f"status: {view.raw_status}",
            f"created_at: {view.created}",
            f"summary: {view.summary}",
        )
    return lines


__all__ = [
    "render_change_applied",
    "render_change_requested",
    "render_human_action",
    "render_project",
]
