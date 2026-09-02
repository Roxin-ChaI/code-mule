"""Deterministic-first and strictly structured Boss intent routing."""

from __future__ import annotations

import re
from typing import Protocol

from code_mule.supervisor.client import SupervisorModelClient
from code_mule.supervisor.contracts import SupervisorOperation

from .contracts import (
    BossIntent,
    BossIntentRouter,
    BossSession,
    InvalidBossIntentResponse,
    RoutedIntent,
)


_EXACT: dict[str, BossIntent] = {
    "status": BossIntent.QUERY_STATUS,
    "状态": BossIntent.QUERY_STATUS,
    "计划是什么": BossIntent.QUERY_PLAN,
    "你的计划是什么": BossIntent.QUERY_PLAN,
    "what is the plan": BossIntent.QUERY_PLAN,
    "现在做到哪了": BossIntent.QUERY_PROGRESS,
    "进度如何": BossIntent.QUERY_PROGRESS,
    "progress": BossIntent.QUERY_PROGRESS,
    "当前在做什么": BossIntent.QUERY_CURRENT_WORK,
    "what are you working on": BossIntent.QUERY_CURRENT_WORK,
    "有什么问题": BossIntent.QUERY_BLOCKERS,
    "有什么阻塞": BossIntent.QUERY_BLOCKERS,
    "blockers": BossIntent.QUERY_BLOCKERS,
    "暂停": BossIntent.PAUSE,
    "先暂停": BossIntent.PAUSE,
    "stop for now": BossIntent.PAUSE,
    "pause": BossIntent.PAUSE,
    "继续": BossIntent.RESUME,
    "恢复": BossIntent.RESUME,
    "resume": BossIntent.RESUME,
    "发生什么了": BossIntent.INSPECT,
    "怎么了": BossIntent.INSPECT,
    "inspect": BossIntent.INSPECT,
    "批准": BossIntent.APPROVE,
    "approve": BossIntent.APPROVE,
    "拒绝": BossIntent.REJECT,
    "reject": BossIntent.REJECT,
    "重试": BossIntent.RESOLVE,
    "retry": BossIntent.RESOLVE,
    "帮助": BossIntent.HELP,
    "help": BossIntent.HELP,
    "?": BossIntent.HELP,
}
_CHANGE_PREFIX = re.compile(
    r"^(?:请)?(?:再)?(?:增加|添加|加入|加上|新增)\s*(.+)$|^(?:please\s+)?(?:add|include)\s+(.+)$",
    re.IGNORECASE,
)
_ACTION_COMMAND = re.compile(
    r"^(?P<command>批准|approve|拒绝|reject|重试|retry)\s+(?P<action_id>\S+)$",
    re.IGNORECASE,
)


class DeterministicBossIntentRouter:
    """Recognize only high-precision Boss phrases without a model call."""

    def route(self, message: str, session: BossSession) -> RoutedIntent:
        normalized = " ".join(message.strip().split())
        if normalized == "":
            return RoutedIntent(BossIntent.UNKNOWN, "clarify", 1.0, "empty input")
        key = normalized.casefold().rstrip("?？。！!")
        action_command = _ACTION_COMMAND.fullmatch(normalized)
        if action_command is not None:
            command = action_command.group("command").casefold()
            intent = {
                "批准": BossIntent.APPROVE,
                "approve": BossIntent.APPROVE,
                "拒绝": BossIntent.REJECT,
                "reject": BossIntent.REJECT,
                "重试": BossIntent.RESOLVE,
                "retry": BossIntent.RESOLVE,
            }[command]
            return RoutedIntent(
                intent,
                action_command.group("action_id"),
                1.0,
                "explicit Human Action ID",
            )
        intent = _EXACT.get(key)
        if intent is not None:
            request = (
                "retry_task" if intent is BossIntent.RESOLVE else normalized
            )
            return RoutedIntent(intent, request)
        change = _CHANGE_PREFIX.fullmatch(normalized)
        if change is not None:
            detail = next(group for group in change.groups() if group is not None)
            if detail.strip():
                return RoutedIntent(
                    BossIntent.CHANGE,
                    normalized,
                    1.0,
                    "explicit change verb",
                )
        if key in {"现在怎么样", "how is it going", "how's it going"}:
            return RoutedIntent(BossIntent.QUERY_STATUS, normalized)
        return RoutedIntent(
            BossIntent.UNKNOWN,
            normalized,
            1.0,
            "no deterministic match",
        )


def boss_intent_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {
                "type": "string",
                "enum": [intent.value for intent in BossIntent],
            },
            "normalized_request": {"type": "string", "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["intent", "normalized_request", "confidence", "reason"],
    }


def parse_boss_intent(payload: dict[str, object]) -> RoutedIntent:
    fields = {"intent", "normalized_request", "confidence", "reason"}
    if set(payload) != fields:
        raise InvalidBossIntentResponse("router response fields do not match contract")
    intent = payload["intent"]
    normalized = payload["normalized_request"]
    confidence = payload["confidence"]
    reason = payload["reason"]
    if not isinstance(intent, str):
        raise InvalidBossIntentResponse("intent must be a string")
    if not isinstance(normalized, str) or normalized == "":
        raise InvalidBossIntentResponse("normalized_request must be non-empty")
    if type(confidence) not in {int, float}:
        raise InvalidBossIntentResponse("confidence must be a number")
    if not isinstance(reason, str) or reason == "":
        raise InvalidBossIntentResponse("reason must be non-empty")
    try:
        return RoutedIntent(BossIntent(intent), normalized, float(confidence), reason)
    except (ValueError, TypeError) as error:
        raise InvalidBossIntentResponse("router response is invalid") from error


class StructuredBossIntentRouter:
    """Use one strict Supervisor model call only after deterministic routing fails."""

    def __init__(self, client: SupervisorModelClient, *, minimum_confidence: float = 0.8):
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between zero and one")
        self._client = client
        self._minimum_confidence = minimum_confidence

    def route(self, message: str, session: BossSession) -> RoutedIntent:
        payload = self._client.create_structured_response(
            operation=SupervisorOperation.BOSS_ROUTING,
            system_prompt=(
                "Classify the Boss message into exactly one schema intent. "
                "You only route; never propose or perform a state change. "
                "Use UNKNOWN for ambiguous requests. normalized_request must "
                "preserve the Boss meaning without adding facts."
            ),
            user_prompt=f"Boss message:\n{message}",
            schema=boss_intent_schema(),
        )
        routed = parse_boss_intent(payload)
        if routed.confidence < self._minimum_confidence:
            return RoutedIntent(
                BossIntent.UNKNOWN,
                routed.normalized_request,
                routed.confidence,
                "model confidence below threshold",
            )
        return routed


class CompositeBossIntentRouter:
    def __init__(
        self,
        deterministic: BossIntentRouter | None = None,
        model: BossIntentRouter | None = None,
    ) -> None:
        self._deterministic = deterministic or DeterministicBossIntentRouter()
        self._model = model

    def route(self, message: str, session: BossSession) -> RoutedIntent:
        routed = self._deterministic.route(message, session)
        if routed.intent is not BossIntent.UNKNOWN or self._model is None:
            return routed
        return self._model.route(message, session)


__all__ = [
    "CompositeBossIntentRouter",
    "DeterministicBossIntentRouter",
    "StructuredBossIntentRouter",
    "boss_intent_schema",
    "parse_boss_intent",
]
