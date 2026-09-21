"""Bounded persistence and read-only projection for Git delivery failures."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from code_mule.domain import HumanActionCategory, HumanActionStatus, ProjectStatus
from code_mule.domain.models import HumanAction

if TYPE_CHECKING:
    from code_mule.state.models import ProjectState

from .contracts import (
    GitDeliveryError,
    GitDeliveryFailureCode,
    GitDeliveryFailureDetails,
    GitOwnershipStatus,
)


def git_delivery_failure_metadata(
    error: GitDeliveryError, stage: str
) -> dict[str, str]:
    """Project one typed failure into bounded event metadata."""

    metadata = {"error_type": type(error).__name__, "stage": stage}
    details = error.details
    if details is None:
        return metadata
    metadata.update(
        {
            "failure_code": details.failure_code.value,
            "baseline_head": details.baseline_head or "-",
            "current_head": details.current_head or "-",
            "expected_paths": _encode_paths(details.expected_paths),
            "actual_paths": _encode_paths(details.actual_paths),
            "staged_paths": _encode_paths(details.staged_paths),
            "task_commit": details.task_commit or "-",
            "ownership_status": details.ownership_status.value,
            "retry_safe": str(details.retry_safe).lower(),
            "safe_summary": details.safe_summary,
        }
    )
    return metadata


def git_delivery_failure_evidence(
    state: ProjectState, action: HumanAction
) -> GitDeliveryFailureDetails | None:
    """Read the exact typed failure associated with one pending HumanAction."""

    if (
        action.category is not HumanActionCategory.RECOVERY_UNCERTAIN
        or action.status is not HumanActionStatus.PENDING
        or action.task_id is None
        or state.project.status is not ProjectStatus.HUMAN_REQUIRED
    ):
        return None
    events = tuple(
        event
        for event in state.events
        if event.event_type == "git.delivery_failed"
        and event.entity_id == action.task_id
        and event.timestamp == action.created_at
        and "failure_code" in event.metadata
    )
    if len(events) != 1:
        return None
    metadata = events[0].metadata
    try:
        return GitDeliveryFailureDetails(
            failure_code=GitDeliveryFailureCode(metadata["failure_code"]),
            baseline_head=_optional(metadata["baseline_head"]),
            current_head=_optional(metadata["current_head"]),
            expected_paths=_decode_paths(metadata["expected_paths"]),
            actual_paths=_decode_paths(metadata["actual_paths"]),
            staged_paths=_decode_paths(metadata["staged_paths"]),
            task_commit=_optional(metadata["task_commit"]),
            ownership_status=GitOwnershipStatus(metadata["ownership_status"]),
            retry_safe={"true": True, "false": False}[metadata["retry_safe"]],
            safe_summary=metadata["safe_summary"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _encode_paths(paths: tuple[str, ...]) -> str:
    return json.dumps(paths, ensure_ascii=True, separators=(",", ":"))


def _decode_paths(raw: str) -> tuple[str, ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Git path diagnostics are invalid")
    return tuple(value)


def _optional(value: str) -> str | None:
    return None if value == "-" else value


__all__ = [
    "git_delivery_failure_evidence",
    "git_delivery_failure_metadata",
]
