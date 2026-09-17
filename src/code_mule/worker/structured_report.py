"""Strict native structured-output contract for Codex execution evidence.

Schema and semantic validation stay strict and perform no repair.  The only
tolerance lives in ``report_contract.extract_report_candidate``, which accepts
the envelope shapes real Codex actually produces (bare JSON, one ```json block,
or one embedded object) and fails closed when a candidate is ambiguous.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import cast

from code_mule.domain.enums import WorkerHumanActionKind
from code_mule.domain.models import WorkerHumanAction
from code_mule.domain.worker_verification import WorkerCheckStatus

from .contracts import CodexWorkerError, WorkerTurnTerminal
from .report_contract import (
    ReportContractError,
    ReportExtractionMode,
    ReportFailureStage,
    ReportValidationCode,
    extract_report_candidate,
)
from code_mule.transport import TransportFailureKind


class WorkerExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class InvalidWorkerReport(CodexWorkerError):
    """Raised when Codex final output violates the worker report schema."""

    transport_failure_kind = TransportFailureKind.REPORT_PARSE_FAILED

    def __init__(
        self,
        message: str,
        *,
        terminal: WorkerTurnTerminal | None = None,
        stage: ReportFailureStage = ReportFailureStage.SCHEMA,
        code: ReportValidationCode = ReportValidationCode.INVALID_SEMANTIC_VALUE,
        field_path: str | None = None,
        candidate_found: bool = False,
        json_decoded: bool = False,
        semantic_validation_started: bool = False,
    ) -> None:
        # A completed turn whose structured report was rejected is *not* a
        # missing terminal result; retain the terminal evidence explicitly.
        self.terminal = terminal
        self.stage = stage
        self.code = code
        self.field_path = field_path
        self.candidate_found = candidate_found
        self.json_decoded = json_decoded
        self.semantic_validation_started = semantic_validation_started
        super().__init__(message)

    @property
    def terminal_received(self) -> bool:
        """Whether a trusted terminal turn event was observed before parsing."""

        return self.terminal is not None

    @property
    def final_message_present(self) -> bool | None:
        """Whether the terminal turn carried a completed final agent message."""

        return None if self.terminal is None else self.terminal.final_message_present


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
    extraction_mode: ReportExtractionMode = ReportExtractionMode.WHOLE_MESSAGE

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
    """Parse exactly one Worker report envelope without repairing its content.

    The envelope may be a bare JSON object, one fenced ```json block, or one
    embedded object.  The decoded object is then validated strictly: no missing
    fields, no extra fields, no coercion, no defaults.
    """

    try:
        extracted = extract_report_candidate(raw_output)
    except ReportContractError as error:
        raise _from_contract_error(error) from None
    value = extracted.value
    mode = extracted.mode
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
        status_text = _string(payload["status"], "worker report.status")
        try:
            status = WorkerExecutionStatus(status_text)
        except ValueError:
            raise _schema_error(
                ReportValidationCode.INVALID_ENUM, "worker report.status"
            ) from None
        git_state = _string(payload["git_state"], "worker report.git_state")
        if git_state not in {"clean", "dirty", "unknown"}:
            raise _schema_error(
                ReportValidationCode.INVALID_ENUM, "worker report.git_state"
            )
        report = StructuredWorkerReport(
            status=status,
            summary=_string(payload["summary"], "worker report.summary"),
            files_changed=_string_tuple(
                payload["files_changed"], "worker report.files_changed"
            ),
            tests=_checks(payload["tests"], "worker report.tests"),
            static_checks=_checks(
                payload["static_checks"], "worker report.static_checks"
            ),
            git_state=git_state,
            issues=_string_tuple(payload["issues"], "worker report.issues"),
            human_action=human_action,
        )
    except TypeError as error:
        raise InvalidWorkerReport(
            "worker report violates its contract",
            stage=ReportFailureStage.SEMANTIC_VALIDATION,
            code=ReportValidationCode.INVALID_SEMANTIC_VALUE,
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        ) from error
    except ReportContractError as error:
        raise _from_contract_error(error) from None
    except ValueError as error:
        raise InvalidWorkerReport(
            "worker report violates its contract",
            stage=ReportFailureStage.SEMANTIC_VALIDATION,
            code=ReportValidationCode.INVALID_SEMANTIC_VALUE,
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        ) from error
    return replace(report, extraction_mode=mode)


def _from_contract_error(error: ReportContractError) -> InvalidWorkerReport:
    return InvalidWorkerReport(
        error.describe(),
        stage=error.stage,
        code=error.code,
        field_path=error.field_path,
        candidate_found=error.candidate_found,
        json_decoded=error.json_decoded,
        semantic_validation_started=error.semantic_validation_started,
    )


def _schema_error(code: ReportValidationCode, field_path: str | None) -> ReportContractError:
    return ReportContractError(
        ReportFailureStage.SCHEMA,
        code,
        field_path=field_path,
        candidate_found=True,
        json_decoded=True,
        semantic_validation_started=True,
    )


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReportContractError(
            ReportFailureStage.SCHEMA,
            ReportValidationCode.NOT_AN_OBJECT,
            field_path=context,
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        )
    return cast(dict[str, object], value)


def _exact_fields(
    payload: dict[str, object], required: set[str], context: str
) -> None:
    missing = required - set(payload)
    extra = set(payload) - required
    if missing:
        first = sorted(missing)[0]
        raise ReportContractError(
            ReportFailureStage.SCHEMA,
            ReportValidationCode.MISSING_FIELD,
            field_path=f"{context}.{first}",
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        )
    if extra:
        first = sorted(extra)[0]
        raise ReportContractError(
            ReportFailureStage.SCHEMA,
            ReportValidationCode.EXTRA_FIELD,
            field_path=f"{context}.{first}",
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        )


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ReportContractError(
            ReportFailureStage.SCHEMA,
            ReportValidationCode.INVALID_FIELD_TYPE,
            field_path=context,
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        )
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ReportContractError(
            ReportFailureStage.SCHEMA,
            ReportValidationCode.INVALID_FIELD_TYPE,
            field_path=context,
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        )
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
            raise ReportContractError(
                ReportFailureStage.SCHEMA,
                ReportValidationCode.INVALID_FIELD_TYPE,
                field_path=f"{item_context}.detail",
                candidate_found=True,
                json_decoded=True,
                semantic_validation_started=True,
            )
        status_text = _string(payload["status"], f"{item_context}.status")
        try:
            status = WorkerCheckStatus(status_text)
        except ValueError:
            raise ReportContractError(
                ReportFailureStage.SCHEMA,
                ReportValidationCode.INVALID_ENUM,
                field_path=f"{item_context}.status",
                candidate_found=True,
                json_decoded=True,
                semantic_validation_started=True,
            ) from None
        if type(payload["required"]) is not bool:
            raise ReportContractError(
                ReportFailureStage.SCHEMA,
                ReportValidationCode.INVALID_CHECK_RESULT,
                field_path=f"{item_context}.required",
                candidate_found=True,
                json_decoded=True,
                semantic_validation_started=True,
            )
        parsed.append(
            WorkerCheckResult(
                name=_string(payload["name"], f"{item_context}.name"),
                status=status,
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
    kind_text = _string(payload["kind"], "worker report.human_action.kind")
    try:
        kind = WorkerHumanActionKind(kind_text)
    except ValueError:
        raise ReportContractError(
            ReportFailureStage.SCHEMA,
            ReportValidationCode.INVALID_HUMAN_ACTION,
            field_path="worker report.human_action.kind",
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        ) from None
    try:
        return WorkerHumanAction(
            kind=kind,
            summary=_string(payload["summary"], "worker report.human_action.summary"),
            request=_string(payload["request"], "worker report.human_action.request"),
            choices=_string_tuple(
                payload["choices"], "worker report.human_action.choices"
            ),
        )
    except ValueError:
        raise ReportContractError(
            ReportFailureStage.SEMANTIC_VALIDATION,
            ReportValidationCode.INVALID_HUMAN_ACTION,
            field_path="worker report.human_action",
            candidate_found=True,
            json_decoded=True,
            semantic_validation_started=True,
        ) from None


__all__ = [
    "InvalidWorkerReport",
    "StructuredWorkerReport",
    "WorkerCheckResult",
    "WorkerCheckStatus",
    "WorkerExecutionStatus",
    "parse_structured_worker_report",
    "structured_worker_report_schema",
]
