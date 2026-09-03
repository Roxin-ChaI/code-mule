"""Line-oriented Boss CLI rendering from immutable view models."""

from code_mule.domain.models import HumanAction
from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationStatus,
)
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
    verification = next(
        (
            result
            for result in reversed(state.project_verification_results)
            if result.plan_id == state.project.active_plan_id
        ),
        None,
    )
    if verification is not None:
        lines += ("", "FINAL VERIFICATION")
        symbols = {
            ProjectVerificationStatus.PASS: "✓",
            ProjectVerificationStatus.FAIL: "✗",
            ProjectVerificationStatus.TIMEOUT: "✗",
            ProjectVerificationStatus.SKIPPED: "–",
            ProjectVerificationStatus.PENDING: "○",
        }
        lines += tuple(
            f"{symbols[check.status]} {check.name}"
            for check in verification.checks
        )
        if verification.final_review_decision is FinalReviewDecision.APPROVE:
            lines += ("✓ Supervisor final review",)
        elif verification.final_review_decision is FinalReviewDecision.HUMAN_REQUIRED:
            lines += ("✗ Supervisor final review",)
        else:
            lines += ("○ Supervisor final review",)
        failed = next(
            (
                check
                for check in verification.checks
                if check.required
                and check.status is not ProjectVerificationStatus.PASS
            ),
            None,
        )
        if failed is not None:
            lines += (f"Failure     {failed.name}: {failed.safe_summary}",)
    elif (
        view.total_tasks > 0
        and view.completed_tasks == view.total_tasks
        and state.project.status.value != "done"
    ):
        lines += (
            "",
            "FINAL VERIFICATION",
            "○ Project checks have not completed.",
            "○ Supervisor final review is pending.",
        )
    if state.project.status.value == "done":
        lines += ("", "PROJECT COMPLETED")
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
            "project_verification_result_id: "
            + ("-" if verification is None else verification.id),
            "final_review_decision: "
            + (
                "-"
                if verification is None or verification.final_review_decision is None
                else verification.final_review_decision.value
            ),
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


def render_project_cancellation_requested(
    state: ProjectState, *, verbose: bool = False
) -> tuple[str, ...]:
    lines = (
        "PROJECT CANCELLATION REQUESTED",
        "",
        "Current task will finish safely.",
        "No new tasks will be dispatched.",
        "Completed work will be preserved.",
    )
    if verbose:
        lines += (
            "",
            f"project_status: {state.project.status.value}",
            f"current_task_id: {state.project.current_task_id or '-'}",
        )
    return lines


def render_project_cancelled(
    state: ProjectState, *, verbose: bool = False
) -> tuple[str, ...]:
    lines = (
        "PROJECT CANCELLED",
        "",
        "Completed work was preserved.",
        "No rollback was performed.",
    )
    if verbose:
        lines += ("", f"project_status: {state.project.status.value}")
    return lines


__all__ = [
    "render_change_applied",
    "render_change_requested",
    "render_human_action",
    "render_project",
    "render_project_cancelled",
    "render_project_cancellation_requested",
]
