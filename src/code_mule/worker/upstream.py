"""Bounded projections of app-server errors; never retain provider prose."""

from dataclasses import dataclass
from datetime import datetime
import json
import re


MAX_UPSTREAM_COUNT = 1_000_000_000
MAX_UPSTREAM_JSON = 4096
STRING_ERROR_CODES = frozenset({
    "contextWindowExceeded", "sessionBudgetExceeded", "usageLimitExceeded",
    "rateLimitExceeded", "serverOverloaded", "cyberPolicy",
    "misalignmentPolicyViolation", "internalServerError", "unauthorized",
    "badRequest", "threadRollbackFailed", "sandboxError", "other",
})
OBJECT_ERROR_CODES = frozenset({
    "httpConnectionFailed", "responseStreamConnectionFailed",
    "responseStreamDisconnected", "responseTooManyFailedAttempts",
    "activeTurnNotSteerable",
})
LEGACY_ERROR_CODES = frozenset({
    "rate_limit_exceeded", "context_window_exceeded", "internal_error",
    "server_error", "model_not_found", "insufficient_quota",
})
SAFE_ERROR_CODES = STRING_ERROR_CODES | OBJECT_ERROR_CODES | LEGACY_ERROR_CODES
UNAVAILABLE = "upstream_detail_unavailable"
_CATEGORY = {
    "responseTooManyFailedAttempts": "upstream_retry_exhaustion",
    "httpConnectionFailed": "upstream_http_failure",
    "responseStreamConnectionFailed": "upstream_stream_connection_failed",
    "responseStreamDisconnected": "upstream_stream_disconnected",
    "rateLimitExceeded": "upstream_rate_limit",
    "rate_limit_exceeded": "upstream_rate_limit",
    "usageLimitExceeded": "upstream_usage_limit",
    "insufficient_quota": "upstream_usage_limit",
    "sessionBudgetExceeded": "session_budget_exceeded",
    "contextWindowExceeded": "context_window_exceeded",
    "context_window_exceeded": "context_window_exceeded",
    "serverOverloaded": "upstream_server_overloaded",
    "internalServerError": "upstream_server_error",
    "internal_error": "upstream_server_error",
    "server_error": "upstream_server_error",
    "unauthorized": "upstream_authentication_failure",
    "badRequest": "upstream_bad_request",
    "model_not_found": "upstream_model_unavailable",
    "sandboxError": "codex_sandbox_failure",
    "threadRollbackFailed": "codex_rollback_failure",
    "activeTurnNotSteerable": "codex_turn_not_steerable",
    "cyberPolicy": "codex_policy_failure",
    "misalignmentPolicyViolation": "codex_policy_failure",
    "other": "codex_other_error",
}
SAFE_CATEGORIES = frozenset(_CATEGORY.values()) | {UNAVAILABLE}


def category_for_code(code: str | None) -> str:
    return _CATEGORY.get(code, UNAVAILABLE)


@dataclass(frozen=True)
class SafeErrorInfo:
    code: str | None = None
    category: str = UNAVAILABLE
    source: str = UNAVAILABLE
    http_status_code: int | None = None

    def __post_init__(self) -> None:
        if self.code is not None and self.code not in SAFE_ERROR_CODES:
            raise ValueError("upstream code is not allowlisted")
        if self.category != _CATEGORY.get(self.code, UNAVAILABLE):
            raise ValueError("upstream category disagrees with code")
        if self.source not in {"code", "codexErrorInfo", UNAVAILABLE}:
            raise ValueError("upstream source is not allowlisted")
        if (self.code is None) != (self.source == UNAVAILABLE):
            raise ValueError("upstream code requires a structured source")
        if self.http_status_code is not None and (
            type(self.http_status_code) is not int
            or not 100 <= self.http_status_code <= 599
            or self.code not in OBJECT_ERROR_CODES - {"activeTurnNotSteerable"}
        ):
            raise ValueError("upstream HTTP status must be a bounded protocol status")


def safe_error_info(error: object) -> SafeErrorInfo:
    if not isinstance(error, dict):
        return SafeErrorInfo()
    info = error.get("codexErrorInfo")
    code = None
    status = None
    source = UNAVAILABLE
    if isinstance(info, str) and info in STRING_ERROR_CODES:
        code, source = info, "codexErrorInfo"
    elif isinstance(info, dict) and len(info) == 1:
        variant, details = next(iter(info.items()))
        if variant in OBJECT_ERROR_CODES and isinstance(details, dict):
            code, source = variant, "codexErrorInfo"
            raw_status = details.get("httpStatusCode")
            if (
                variant != "activeTurnNotSteerable"
                and type(raw_status) is int and 100 <= raw_status <= 599
            ):
                status = raw_status
    if code is None:
        legacy = error.get("code")
        if isinstance(legacy, str) and legacy in LEGACY_ERROR_CODES:
            code, source = legacy, "code"
    return SafeErrorInfo(code, _CATEGORY.get(code, UNAVAILABLE), source, status)


@dataclass(frozen=True)
class UpstreamErrorObservation:
    at: datetime
    retryable: bool | None
    info: SafeErrorInfo
    thread_id: str
    turn_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.at, datetime) or self.at.utcoffset() is None:
            raise ValueError("upstream timestamp must be aware")
        if self.retryable is not None and type(self.retryable) is not bool:
            raise ValueError("upstream retryable must be boolean or unavailable")
        if not isinstance(self.info, SafeErrorInfo):
            raise ValueError("upstream info must be typed")
        for identity in (self.thread_id, self.turn_id):
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                raise ValueError("upstream identity must be bounded")

    def to_payload(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(), "retryable": self.retryable,
            "code": self.info.code, "category": self.info.category,
            "source": self.info.source, "http_status_code": self.info.http_status_code,
            "thread_id": self.thread_id, "turn_id": self.turn_id,
        }


@dataclass(frozen=True)
class UpstreamErrorSummary:
    count: int
    first: UpstreamErrorObservation
    last: UpstreamErrorObservation

    def __post_init__(self) -> None:
        if type(self.count) is not int or not 1 <= self.count <= MAX_UPSTREAM_COUNT:
            raise ValueError("upstream count must be bounded")
        if not all(isinstance(v, UpstreamErrorObservation) for v in (self.first, self.last)):
            raise ValueError("upstream observations must be typed")
        if self.first.at > self.last.at:
            raise ValueError("upstream timestamps are out of order")
        if self.count == 1 and self.first != self.last:
            raise ValueError("one upstream error requires identical observations")
        if (self.first.thread_id, self.first.turn_id, self.first.retryable is True) != (
            self.last.thread_id, self.last.turn_id, self.last.retryable is True
        ):
            raise ValueError("upstream summary mixes identities or retry buckets")

    def append(self, observation: UpstreamErrorObservation) -> "UpstreamErrorSummary":
        return UpstreamErrorSummary(min(self.count + 1, MAX_UPSTREAM_COUNT), self.first, observation)

    def to_payload(self) -> dict[str, object]:
        return {"count": self.count, "first": self.first.to_payload(), "last": self.last.to_payload()}


@dataclass(frozen=True)
class UpstreamDiagnostics:
    retryable: UpstreamErrorSummary | None = None
    final: UpstreamErrorSummary | None = None

    def __post_init__(self) -> None:
        for summary, retryable in ((self.retryable, True), (self.final, False)):
            if summary is not None and (
                not isinstance(summary, UpstreamErrorSummary)
                or (summary.last.retryable is True) != retryable
            ):
                raise ValueError("upstream summary has the wrong retry bucket")
        if self.retryable is not None and self.final is not None and (
            self.retryable.last.thread_id, self.retryable.last.turn_id
        ) != (self.final.last.thread_id, self.final.last.turn_id):
            raise ValueError("upstream diagnostics mix turn identities")

    def to_payload(self) -> dict[str, object]:
        return {name: None if value is None else value.to_payload()
                for name, value in (("retryable", self.retryable), ("final", self.final))}

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), separators=(",", ":"))


def upstream_from_json(value: object) -> UpstreamDiagnostics | None:
    """Revalidate persisted metadata and discard all unrecognized fields."""
    if not isinstance(value, str) or len(value) > MAX_UPSTREAM_JSON:
        return None
    try:
        payload = json.loads(value)
        def observation(raw):
            return UpstreamErrorObservation(
                datetime.fromisoformat(raw["at"]), raw["retryable"],
                SafeErrorInfo(raw["code"], raw["category"], raw["source"], raw["http_status_code"]),
                raw["thread_id"], raw["turn_id"],
            )
        def summary(raw):
            if raw is None:
                return None
            return UpstreamErrorSummary(raw["count"], observation(raw["first"]), observation(raw["last"]))
        return UpstreamDiagnostics(summary(payload["retryable"]), summary(payload["final"]))
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, RecursionError):
        return None
