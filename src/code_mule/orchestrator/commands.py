"""Boss command contracts accepted by the Orchestrator."""

from dataclasses import dataclass


def _require_non_empty(value: str, field_name: str) -> None:
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True)
class QueryCommand:
    project_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")


@dataclass(frozen=True)
class ChangeCommand:
    project_id: str
    description: str
    created_by: str
    change_request_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.description, "description")
        _require_non_empty(self.created_by, "created_by")
        _require_non_empty(self.change_request_id, "change_request_id")


@dataclass(frozen=True)
class PauseCommand:
    project_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")


@dataclass(frozen=True)
class ResumeCommand:
    project_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")


@dataclass(frozen=True)
class StopCommand:
    project_id: str
    reason: str = "Boss requested project cancellation"

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.reason, "reason")


__all__ = [
    "ChangeCommand",
    "PauseCommand",
    "QueryCommand",
    "ResumeCommand",
    "StopCommand",
]
