"""Typed delivery checks; legacy text is conservative, never optional by inference."""

from dataclasses import dataclass
from enum import StrEnum
import re


class WorkerCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"


class WorkerCheckType(StrEnum):
    TEST = "test"
    STATIC_CHECK = "static_check"


@dataclass(frozen=True)
class WorkerVerificationCheck:
    name: str
    check_type: WorkerCheckType
    status: WorkerCheckStatus
    required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or len(self.name) > 200:
            raise ValueError("verification check name must have 1..200 characters")
        if not isinstance(self.check_type, WorkerCheckType):
            raise ValueError("verification check type must be typed")
        if not isinstance(self.status, WorkerCheckStatus) or type(self.required) is not bool:
            raise ValueError("verification status/required must be typed")

    @property
    def permits_delivery(self) -> bool:
        return self.status is WorkerCheckStatus.PASS or (
            not self.required and self.status is WorkerCheckStatus.NOT_RUN
        )


def legacy_checks(tests: tuple[str, ...], static_checks: tuple[str, ...]) -> tuple[WorkerVerificationCheck, ...]:
    """Read the historical formatter's exact grammar, defaulting to required.

    Unrecognized evidence remains UNKNOWN; a ': pass' inside detail is not a pass.
    This adapter is only for reports without typed evidence (pre-v11/local callers).
    """
    result = []
    for kind, entries in ((WorkerCheckType.TEST, tests), (WorkerCheckType.STATIC_CHECK, static_checks)):
        for entry in entries:
            match = re.fullmatch(r"([^\n]{1,200}?): (pass|fail|not_run|unknown)(?: \([^\n]*\))?", entry)
            result.append(WorkerVerificationCheck(
                match[1] if match else "Unrecognized legacy check",
                kind,
                WorkerCheckStatus(match[2]) if match else WorkerCheckStatus.UNKNOWN,
                True,
            ))
    return tuple(result)


def evidence_matches_text(checks: tuple[WorkerVerificationCheck, ...], tests: tuple[str, ...], static_checks: tuple[str, ...]) -> bool:
    """Prevent duplicate display fields from contradicting typed delivery evidence."""
    for kind, entries in ((WorkerCheckType.TEST, tests), (WorkerCheckType.STATIC_CHECK, static_checks)):
        selected = tuple(check for check in checks if check.check_type is kind)
        if len(selected) != len(entries):
            return False
        for check, text in zip(selected, entries, strict=True):
            prefix = f"{check.name}: {check.status.value}"
            if text != prefix and not (text.startswith(prefix + " (") and text.endswith(")")):
                return False
    return True
