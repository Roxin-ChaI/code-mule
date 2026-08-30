"""Deterministic project-state transition contract."""

from .enums import ProjectStatus


class InvalidProjectTransition(ValueError):
    """Raised when a project-state transition is not allowed."""


_ALLOWED_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = {
    ProjectStatus.IDLE: frozenset({ProjectStatus.PLANNING}),
    ProjectStatus.PLANNING: frozenset(
        {
            ProjectStatus.RUNNING,
            ProjectStatus.HUMAN_REQUIRED,
            ProjectStatus.FAILED,
        }
    ),
    ProjectStatus.RUNNING: frozenset(
        {
            ProjectStatus.RUNNING,
            ProjectStatus.CHANGE_REQUESTED,
            ProjectStatus.PAUSED_BY_BOSS,
            ProjectStatus.HUMAN_REQUIRED,
            ProjectStatus.DONE,
            ProjectStatus.FAILED,
        }
    ),
    ProjectStatus.CHANGE_REQUESTED: frozenset(
        {
            ProjectStatus.REPLANNING,
            ProjectStatus.HUMAN_REQUIRED,
            ProjectStatus.FAILED,
        }
    ),
    ProjectStatus.REPLANNING: frozenset(
        {
            ProjectStatus.RUNNING,
            ProjectStatus.HUMAN_REQUIRED,
            ProjectStatus.FAILED,
        }
    ),
    ProjectStatus.PAUSED_BY_BOSS: frozenset(
        {
            ProjectStatus.RUNNING,
            ProjectStatus.CHANGE_REQUESTED,
        }
    ),
    ProjectStatus.HUMAN_REQUIRED: frozenset(
        {
            ProjectStatus.RUNNING,
            ProjectStatus.PAUSED_BY_BOSS,
            ProjectStatus.FAILED,
        }
    ),
    ProjectStatus.DONE: frozenset(),
    ProjectStatus.FAILED: frozenset(),
}


def can_transition(current: ProjectStatus, target: ProjectStatus) -> bool:
    """Return whether the requested project-state transition is legal."""

    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def validate_transition(current: ProjectStatus, target: ProjectStatus) -> None:
    """Raise when the requested project-state transition is illegal."""

    if not can_transition(current, target):
        raise InvalidProjectTransition(
            f"invalid project transition: {current} -> {target}"
        )


__all__ = ["InvalidProjectTransition", "can_transition", "validate_transition"]
