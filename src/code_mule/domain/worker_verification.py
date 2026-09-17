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


# Delivery-blocking checks are surfaced in bounded metadata; never unbounded.
UNMET_CHECK_LIMIT = 5


def report_verification_checks(report) -> tuple[WorkerVerificationCheck, ...]:
    """Typed delivery evidence for one report, or its legacy text adaption."""

    checks = getattr(report, "verification_checks", None)
    if checks is None:
        return legacy_checks(report.tests, report.static_checks)
    return checks


def unmet_required_checks(
    checks: tuple[WorkerVerificationCheck, ...],
) -> tuple[WorkerVerificationCheck, ...]:
    """Required checks that did not actually pass and therefore block delivery."""

    return tuple(
        check
        for check in checks
        if check.required and check.status is not WorkerCheckStatus.PASS
    )


def blocking_checks(
    checks: tuple[WorkerVerificationCheck, ...],
) -> tuple[WorkerVerificationCheck, ...]:
    """Every check that forbids delivery, matching the delivery gate exactly.

    A required check blocks unless it passed; an optional check blocks unless
    it passed or is explicitly not_run.
    """

    return tuple(check for check in checks if not check.permits_delivery)


def blocking_check_metadata(
    checks: tuple[WorkerVerificationCheck, ...],
) -> dict[str, str]:
    """Bounded, label-only metadata naming every delivery-blocking check.

    The first blocking check also fills the historical single-check keys so
    existing diagnosis and rendering keep working unchanged.
    """

    metadata: dict[str, str] = {"unmet_check_count": str(len(checks))}
    if not checks:
        return metadata
    first = checks[0]
    metadata.update(
        {
            "check_name": safe_check_name(first.name),
            "check_type": first.check_type.value,
            "check_status": first.status.value,
            "check_required": str(first.required).lower(),
        }
    )
    for index, check in enumerate(checks[:UNMET_CHECK_LIMIT], start=1):
        metadata[f"unmet_check_{index}_name"] = safe_check_name(check.name)
        metadata[f"unmet_check_{index}_type"] = check.check_type.value
        metadata[f"unmet_check_{index}_status"] = check.status.value
        metadata[f"unmet_check_{index}_required"] = str(check.required).lower()
    return metadata


def unmet_check_summary(
    checks: tuple[WorkerVerificationCheck, ...],
) -> str:
    """Bounded one-line summary, safe for a Human Gate summary field."""

    if not checks:
        return "No required check was unexecuted"
    labels = ", ".join(
        f"{check.check_type.value} {safe_check_name(check.name)} ({check.status.value})"
        for check in checks[:UNMET_CHECK_LIMIT]
    )
    suffix = "" if len(checks) <= UNMET_CHECK_LIMIT else f" (+{len(checks) - UNMET_CHECK_LIMIT} more)"
    return f"Unexecuted required checks: {labels}{suffix}"[:300]


def safe_check_name(name: str) -> str:
    """Bounded label-only diagnostics; never copy a command, payload or detail.

    Suspicious labels are replaced wholesale, not partially redacted. This is
    an output policy, not a classifier and never influences delivery decisions.
    """
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff ._-]{1,120}", name)
        or re.search(r"secret|token|password|credential|api.?key|authorization|bearer|sk-|[A-Za-z0-9_-]{33,}", name, re.I)
    ):
        return "[check name withheld]"
    return name.strip() or "[check name withheld]"
