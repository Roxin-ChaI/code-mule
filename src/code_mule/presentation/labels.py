"""Stable human-readable labels for internal control values."""

from enum import Enum

from code_mule.domain.enums import ProjectStatus, SupervisorDecisionType


_PROJECT_STATUS_LABELS = {
    ProjectStatus.IDLE.value: "Ready",
    ProjectStatus.PLANNING.value: "Planning",
    ProjectStatus.RUNNING.value: "Running",
    ProjectStatus.CHANGE_REQUESTED.value: "Change requested",
    ProjectStatus.REPLANNING.value: "Replanning",
    ProjectStatus.CANCEL_REQUESTED.value: "Cancellation requested",
    ProjectStatus.PAUSED_BY_BOSS.value: "Paused",
    ProjectStatus.HUMAN_REQUIRED.value: "Action required",
    ProjectStatus.DONE.value: "Completed",
    ProjectStatus.FAILED.value: "Failed",
    ProjectStatus.CANCELLED.value: "Cancelled",
}

_DECISION_LABELS = {
    SupervisorDecisionType.CONTINUE.value: "Approved",
    SupervisorDecisionType.REWORK.value: "Changes requested",
    SupervisorDecisionType.HUMAN_REQUIRED.value: "Action required",
    SupervisorDecisionType.DONE.value: "Completed",
}


def _value(value: str | Enum) -> str:
    return str(value.value) if isinstance(value, Enum) else value


def humanize_identifier(value: str) -> str:
    return value.replace("_", " ").strip().capitalize()


def status_label(value: str | ProjectStatus) -> str:
    raw = _value(value)
    return _PROJECT_STATUS_LABELS.get(raw, humanize_identifier(raw))


def decision_label(value: str | SupervisorDecisionType) -> str:
    raw = _value(value)
    return _DECISION_LABELS.get(raw, humanize_identifier(raw))


__all__ = ["decision_label", "humanize_identifier", "status_label"]
