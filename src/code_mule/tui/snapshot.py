"""Project persisted truth into the three terminal panes.

Every line is derived from ``ProjectState`` (plus the bounded activity log), so
the terminal never holds a second copy of business truth.
"""

from dataclasses import dataclass
from datetime import datetime

from code_mule.domain.enums import HumanActionStatus
from code_mule.presentation.labels import humanize_identifier
from code_mule.presentation.models import human_action_view, project_view
from code_mule.presentation.terminal import safe_text
from code_mule.revision import latest_revision
from code_mule.state.models import ProjectState


def _bounded(value: object, limit: int = 200) -> str:
    return safe_text(str(value))[:limit]


def runtime_lines(state: ProjectState) -> tuple[str, ...]:
    """Compact runtime summary; empty when no session was ever recorded."""

    if not state.runtime_sessions:
        return ()
    session = state.runtime_sessions[-1]
    health = session.health_status.value.replace("_", " ")
    status = session.status.value.replace("_", " ")
    where = session.access_url or "-"
    return (f"Runtime     {status} / {health} · {_bounded(where, 120)}",)


def status_lines(state: ProjectState, *, verbose: bool = False) -> tuple[str, ...]:
    """Fixed Status pane content: project, work, safe point, runtime."""

    view = project_view(state)
    revision = latest_revision(state)
    task = next(
        (item for item in state.tasks if item.id == state.project.current_task_id),
        None,
    )
    plan = "—" if view.plan_version is None else f"v{view.plan_version}"
    revision_text = (
        "—" if revision is None else str(revision.revision_number)
    )
    lines = (
        f"CODE MULE · {_bounded(view.name)}",
        f"Project     {_bounded(view.name)}",
        f"Revision    {revision_text}    Plan {plan}",
        f"Status      {view.status}",
        f"Progress    {view.completed_tasks} / {view.total_tasks}",
        "Current     " + ("None" if task is None else _bounded(task.title, 120)),
        "Safe Point  "
        + humanize_identifier(
            state.latest_safe_point.kind.value if state.latest_safe_point else "unknown"
        ),
    )
    lines += runtime_lines(state)
    if verbose:
        lines += (f"Project ID  {_bounded(state.project.id, 80)}",)
    return lines


def boss_lines(
    state: ProjectState,
    *,
    pending: bool,
    buffer: str,
    status_message: str | None = None,
) -> tuple[str, ...]:
    """Fixed Boss pane content: a compact HumanAction summary, then the input.

    The pane stays readable: one short line per fact, no expanded
    ``resolve <id> --strategy ...`` command.  Action ids and full commands stay
    discoverable through the Activity pane and ``inspect``.
    """

    lines: list[str] = []
    action = _pending_action(state)
    if action is not None:
        view = human_action_view(action)
        lines.append(f"HUMAN ACTION · {_bounded(view.category, 60)}")
        lines.append(f"Request: {_bounded(view.request, 200)}")
        choices = _choice_summary(view)
        if choices:
            lines.append(f"Choices: {choices}")
        lines.append("Actions: " + _action_summary(state, action))
    if status_message:
        lines.append(_bounded(status_message, 200))
    prompt = "boss> " + safe_text(buffer)
    lines.append(prompt)
    return tuple(lines)


def _pending_action(state: ProjectState):
    matches = tuple(
        action
        for action in state.human_actions
        if action.status is HumanActionStatus.PENDING
    )
    return matches[-1] if len(matches) == 1 else None


def _action_summary(state: ProjectState, action) -> str:
    """One short line naming the available Boss actions."""

    try:
        from code_mule.human import allowed_resolution_strategies

        strategies = allowed_resolution_strategies(state, action)
    except Exception:
        strategies = ()
    names = [strategy.value for strategy in strategies]
    if action.category.value == "worker_input":
        names.append("answer")
    if not names:
        return "inspect"
    return " | ".join(names)


def _choice_summary(view) -> str | None:
    """Compact, bounded choice list for a Worker input question."""

    if not view.choices:
        return None
    joined = " | ".join(_bounded(choice, 40) for choice in view.choices[:6])
    suffix = "" if len(view.choices) <= 6 else f" (+{len(view.choices) - 6} more)"
    return (joined + suffix)[:200]


@dataclass(frozen=True)
class TerminalSnapshot:
    """One immutable frame of already-projected pane content."""

    status: tuple[str, ...]
    activity: tuple[str, ...]
    boss: tuple[str, ...]
    generated_at: datetime

    def pane(self, name: str) -> tuple[str, ...]:
        if name == "status":
            return self.status
        if name == "activity":
            return self.activity
        if name == "boss":
            return self.boss
        raise ValueError("unknown pane")


def build_snapshot(
    state: ProjectState | None,
    activity: tuple[str, ...],
    *,
    buffer: str = "",
    status_message: str | None = None,
    verbose: bool = False,
    now: datetime,
) -> TerminalSnapshot:
    """Assemble one frame from persisted state and the conversation log."""

    if state is None:
        return TerminalSnapshot(
            (
                "CODE MULE",
                "Project     Not initialized",
                "Status      No project state found",
            ),
            activity,
            ("boss> " + safe_text(buffer),)
            + (() if status_message is None else (_bounded(status_message),)),
            now,
        )
    return TerminalSnapshot(
        status_lines(state, verbose=verbose),
        activity,
        boss_lines(
            state,
            pending=True,
            buffer=buffer,
            status_message=status_message,
        ),
        now,
    )


__all__ = [
    "TerminalSnapshot",
    "boss_lines",
    "build_snapshot",
    "runtime_lines",
    "status_lines",
]
