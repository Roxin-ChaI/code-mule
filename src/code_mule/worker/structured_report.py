"""Strict native structured-output contract for Codex execution evidence."""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from code_mule.domain.enums import WorkerHumanActionKind
from code_mule.domain.models import WorkerHumanAction
from code_mule.domain.worker_verification import WorkerCheckStatus

from .contracts import CodexWorkerError


class WorkerExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class InvalidWorkerReport(CodexWorkerError):
    """Raised when Codex final output violates the worker report schema."""


@dataclass(frozen=True)
class WorkerCheckResult:
    name: str
    status: WorkerCheckStatus
    detail: str | None
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 200:
            raise ValueError("name must have 1..200 characters")
        if type(self.required) is not bool or not isinstance(self.status, WorkerCheckStatus):
            raise ValueError("required/status must be typed")


@dataclass(frozen=True)
class StructuredWorkerReport:
    status: WorkerExecutionStatus
    summary: str
    files_changed: tuple[str, ...]
    tests: tuple[WorkerCheckResult, ...]
    static_checks: tuple[WorkerCheckResult, ...]
    git_state: str
    issues: tuple[str, ...]
    human_action: WorkerHumanAction | None

    def __post_init__(self) -> None:
        if self.git_state not in {"clean", "dirty", "unknown"}:
            raise ValueError("git_state must be clean, dirty, or unknown")
        if self.human_action is not None and not isinstance(self.human_action, WorkerHumanAction):
            raise ValueError("human_action must be a typed WorkerHumanAction or None")

    @property
    def human_action_required(self) -> bool:
        return self.human_action is not None


def structured_worker_report_schema() -> dict[str, object]:
    """Return a fresh strict JSON Schema for app-server outputSchema."""

    check_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "status": {
                "type": "string",
                "enum": [item.value for item in WorkerCheckStatus],
            },
            "detail": {"type": ["string", "null"]},
            "required": {"type": "boolean"},
        },
        "required": ["name", "status", "detail", "required"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": [item.value for item in WorkerExecutionStatus],
            },
            "summary": {"type": "string"},
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "tests": {"type": "array", "items": check_schema},
            "static_checks": {"type": "array", "items": check_schema},
            "git_state": {
                "type": "string",
                "enum": ["clean", "dirty", "unknown"],
            },
            "issues": {"type": "array", "items": {"type": "string"}},
            "human_action": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [item.value for item in WorkerHumanActionKind],
                            },
                            "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "request": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "choices": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string", "minLength": 1, "maxLength": 500},
                            },
                        },
                        "required": ["kind", "summary", "request", "choices"],
                    },
                ]
            },
        },
        "required": [
            "status",
            "summary",
            "files_changed",
            "tests",
            "static_checks",
            "git_state",
            "issues",
            "human_action",
        ],
    }


def parse_structured_worker_report(raw_output: str) -> StructuredWorkerReport:
    """Parse exactly one JSON object without repair, extraction, or defaults."""

    if not isinstance(raw_output, str):
        raise InvalidWorkerReport("worker report output must be a string")
    try:
        value: object = json.loads(raw_output)
    except json.JSONDecodeError as error:
        raise InvalidWorkerReport("worker report is not valid JSON") from error
    try:
        payload = _object(value, "worker report")
        _exact_fields(
            payload,
            {
                "status",
                "summary",
                "files_changed",
                "tests",
                "static_checks",
                "git_state",
                "issues",
                "human_action",
            },
            "worker report",
        )
        human_action = _human_action(payload["human_action"])
        return StructuredWorkerReport(
            status=WorkerExecutionStatus(_string(payload["status"], "status")),
            summary=_string(payload["summary"], "summary"),
            files_changed=_string_tuple(payload["files_changed"], "files_changed"),
            tests=_checks(payload["tests"], "tests"),
            static_checks=_checks(payload["static_checks"], "static_checks"),
            git_state=_string(payload["git_state"], "git_state"),
            issues=_string_tuple(payload["issues"], "issues"),
            human_action=human_action,
        )
    except InvalidWorkerReport:
        raise
    except (TypeError, ValueError) as error:
        raise InvalidWorkerReport("worker report violates its contract") from error


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InvalidWorkerReport(f"{context} must be an object")
    return cast(dict[str, object], value)


def _exact_fields(
    payload: dict[str, object], required: set[str], context: str
) -> None:
    missing = required - set(payload)
    extra = set(payload) - required
    if missing:
        raise InvalidWorkerReport(
            f"{context} is missing fields: {', '.join(sorted(missing))}"
        )
    if extra:
        raise InvalidWorkerReport(
            f"{context} contains extra fields: {', '.join(sorted(extra))}"
        )


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkerReport(f"{context} must be a string")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidWorkerReport(f"{context} must be an array")
    return cast(list[object], value)


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{context}[{index}]")
        for index, item in enumerate(_array(value, context))
    )


def _checks(value: object, context: str) -> tuple[WorkerCheckResult, ...]:
    parsed: list[WorkerCheckResult] = []
    for index, item in enumerate(_array(value, context)):
        item_context = f"{context}[{index}]"
        payload = _object(item, item_context)
        _exact_fields(payload, {"name", "status", "detail", "required"}, item_context)
        detail = payload["detail"]
        if detail is not None and not isinstance(detail, str):
            raise InvalidWorkerReport(f"{item_context}.detail must be string or null")
        parsed.append(
            WorkerCheckResult(
                name=_string(payload["name"], f"{item_context}.name"),
                status=WorkerCheckStatus(
                    _string(payload["status"], f"{item_context}.status")
                ),
                detail=detail,
                required=payload["required"],
            )
        )
    return tuple(parsed)


def _human_action(value: object) -> WorkerHumanAction | None:
    if value is None:
        return None
    payload = _object(value, "worker report.human_action")
    _exact_fields(
        payload,
        {"kind", "summary", "request", "choices"},
        "worker report.human_action",
    )
    return WorkerHumanAction(
        kind=WorkerHumanActionKind(
            _string(payload["kind"], "worker report.human_action.kind")
        ),
        summary=_string(payload["summary"], "worker report.human_action.summary"),
        request=_string(payload["request"], "worker report.human_action.request"),
        choices=_string_tuple(payload["choices"], "worker report.human_action.choices"),
    )


__all__ = [
    "InvalidWorkerReport",
    "StructuredWorkerReport",
    "WorkerCheckResult",
    "WorkerCheckStatus",
    "WorkerExecutionStatus",
    "parse_structured_worker_report",
    "structured_worker_report_schema",
]
