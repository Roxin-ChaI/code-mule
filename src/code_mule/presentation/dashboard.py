"""Pure terminal-dashboard rendering from ephemeral progress snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_mule.progress.contracts import ProgressEvent, ProgressSnapshot

from .labels import decision_label, humanize_identifier, status_label


_COMPLETED_EVENTS = frozenset(
    {
        "task.completed",
        "worker.completed",
        "supervisor.review_completed",
        "supervisor.plan_completed",
        "supervisor.impact_completed",
        "planning.completed",
        "replanning.completed",
        "project.completed",
    }
)
_ACTIVE_EVENTS = frozenset(
    {
        "task.dispatched",
        "task.started",
        "worker.starting",
        "worker.started",
        "worker.activity",
        "supervisor.review_started",
        "supervisor.plan_started",
        "supervisor.impact_started",
        "planning.started",
        "replanning.started",
    }
)


def render_dashboard(
    snapshot: ProgressSnapshot,
    *,
    project_elapsed: str,
    stage_elapsed: str,
    spinner: str,
    final: bool,
    width: int,
    ascii_only: bool,
) -> list[str]:
    width = max(24, width)
    dash = "-" if ascii_only else "─"
    separator = dash * min(width, 48)
    empty = "-" if ascii_only else "—"
    worker = humanize_identifier(snapshot.worker_status)
    supervisor = _supervisor_label(snapshot.supervisor_status)
    if not final and worker == "Working":
        worker = f"{spinner} Working {'-' if ascii_only else '·'} {stage_elapsed}"
    if not final and snapshot.supervisor_status in {
        "Reviewing",
        "Planning",
        "Analyzing impact",
        "Materializing",
    }:
        supervisor = f"{spinner} {supervisor} · {stage_elapsed}"

    progress = _progress(snapshot, width=width, ascii_only=ascii_only)
    task = snapshot.current_task_title or snapshot.current_task_id or empty
    lines = [
        "Code Mule",
        separator,
        "",
        "PROJECT",
        f"Project     {snapshot.project_name or snapshot.project_id or empty}",
        f"Status      {status_label(snapshot.project_status or 'waiting')}",
        f"Plan        {empty if snapshot.plan_version is None else f'v{snapshot.plan_version}'}",
        f"Progress    {progress}",
        f"Elapsed     {project_elapsed}",
        "",
        "CURRENT TASK",
        f"Task        {_task_line(snapshot.current_task_id, task, empty, ascii_only)}",
        f"Attempt     {snapshot.current_attempt or empty}",
        "",
        "WORKER",
        f"Codex       {worker}",
        f"Activity    {snapshot.worker_activity or empty}",
        f"Elapsed     {stage_elapsed if snapshot.worker_status == 'Working' else empty}",
        "",
        "SUPERVISOR",
        f"DeepSeek    {supervisor}",
        "",
        "RECENT ACTIVITY",
    ]
    if snapshot.recent_events:
        lines.extend(
            _recent_line(event, ascii_only=ascii_only)
            for event in snapshot.recent_events
        )
    else:
        lines.append(empty)
    lines += (
        ["", "! ACTION REQUIRED", "No action has been executed."]
        if snapshot.project_status == "human_required"
        else ["", "No action required."]
    )
    return [_fit(line, width=width, ascii_only=ascii_only) for line in lines]


def _progress(snapshot: ProgressSnapshot, *, width: int, ascii_only: bool) -> str:
    if snapshot.project_status in {"planning", "replanning"} and snapshot.total_tasks == 0:
        return status_label(snapshot.project_status)
    bar_width = 8 if width < 48 else 16
    filled = int(snapshot.percentage / 100 * bar_width)
    if ascii_only:
        bar = "#" * filled + "-" * (bar_width - filled)
    else:
        bar = "█" * filled + "░" * (bar_width - filled)
    return f"{bar}  {snapshot.completed_tasks} / {snapshot.total_tasks}"


def _task_line(
    task_id: str | None, task: str, empty: str, ascii_only: bool
) -> str:
    if task_id is None:
        return task
    if task == task_id:
        return task_id
    return f"{task_id} {'-' if ascii_only else '·'} {task}"


def _supervisor_label(value: str) -> str:
    raw = value.lower()
    if raw in {"continue", "rework", "human_required", "done"}:
        return decision_label(raw)
    return humanize_identifier(value)


def _recent_line(event: ProgressEvent, *, ascii_only: bool) -> str:
    event_type = event.type.value
    if event_type in _COMPLETED_EVENTS:
        symbol = "[OK]" if ascii_only else "✓"
    elif event_type in _ACTIVE_EVENTS:
        symbol = ">" if ascii_only else "→"
    elif event_type in {
        "runtime.human_gate",
        "task.human_required",
        "runtime.error",
        "worker.failed",
        "supervisor.failed",
        "planning.failed",
        "replanning.failed",
    }:
        symbol = "!"
    else:
        symbol = "·" if not ascii_only else "-"
    message = event.message or humanize_identifier(event.type.value)
    return f"{symbol} {message}"


def _fit(value: str, *, width: int, ascii_only: bool) -> str:
    if len(value) <= width:
        return value
    placeholder = "..." if ascii_only else "…"
    return value[: max(0, width - len(placeholder))] + placeholder


__all__ = ["render_dashboard"]
