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
    "项目诊断": BossIntent.DIAGNOSE,
    "为什么停了": BossIntent.DIAGNOSE,
    "为什么停止了": BossIntent.DIAGNOSE,
    "现在卡在哪里": BossIntent.DIAGNOSE,
    "现在卡在哪": BossIntent.DIAGNOSE,
    "为什么不能继续": BossIntent.DIAGNOSE,
    "我需要做什么": BossIntent.DIAGNOSE,
    "怎么继续": BossIntent.DIAGNOSE,
    "diagnose project": BossIntent.DIAGNOSE,
    "why did the project stop": BossIntent.DIAGNOSE,
    "why is it blocked": BossIntent.DIAGNOSE,
    "what is blocking the project": BossIntent.DIAGNOSE,
    "what should i do next": BossIntent.DIAGNOSE,
    "how can i continue": BossIntent.DIAGNOSE,
    "最终验证结果是什么": BossIntent.QUERY_STATUS,
    "为什么项目还没完成": BossIntent.QUERY_STATUS,
    "暂停": BossIntent.PAUSE,
    "先暂停": BossIntent.PAUSE,
    "stop for now": BossIntent.PAUSE,
    "pause": BossIntent.PAUSE,
    "停止项目": BossIntent.STOP,
    "不做了": BossIntent.STOP,
    "这个项目不做了": BossIntent.STOP,
    "取消这个项目": BossIntent.STOP,
    "stop project": BossIntent.STOP,
    "cancel project": BossIntent.STOP,
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
_MATCH_PUNCTUATION = str.maketrans(
    {character: " " for character in "?？。!！,，;；:：、…()（）"}
)
_READ_ONLY_PATTERNS: tuple[
    tuple[BossIntent, tuple[re.Pattern[str], ...]], ...
] = (
    (
        BossIntent.QUERY_STATUS,
        (
            re.compile(r"最终验证(?:结果)?(?:是)?什么"),
            re.compile(r"为什么项目还没完成"),
        ),
    ),
    (
        BossIntent.QUERY_BLOCKERS,
        (
            re.compile(r"(?:有什么|有没有(?:什么)?)(?:问题|阻塞)(?:需要我(?:来)?(?:处理|介入))?"),
            re.compile(r"卡在(?:哪|哪里|什么地方)"),
            re.compile(r"(?:有什么|有没有什么)?需要我(?:来)?(?:处理|介入)"),
            re.compile(r"我需要做什么"),
        ),
    ),
    (
        BossIntent.QUERY_CURRENT_WORK,
        (
            re.compile(r"(?:现在|当前)(?:在|正)?(?:干|做|忙)(?:什么|啥)"),
            re.compile(r"正在做(?:哪个|什么)?任务"),
        ),
    ),
    (
        BossIntent.QUERY_PROGRESS,
        (
            re.compile(r"做到哪(?:一步|里|儿|了)?"),
            re.compile(r"进度(?:如何|怎么样|怎样|到哪)?"),
            re.compile(r"完成(?:了)?(?:多少|几个)"),
            re.compile(r"(?:还有|还剩|剩余)(?:几个|多少)?(?:个)?(?:任务|工作)"),
        ),
    ),
    (
        BossIntent.QUERY_PLAN,
        (
            re.compile(r"(?:完整)?计划(?:是)?什么"),
            re.compile(r"怎么安排(?:的)?"),
            re.compile(r"分(?:成)?几个步骤"),
            re.compile(r"接下来(?:准备)?(?:怎么做|做什么|干什么)"),
        ),
    ),
)
_SIDE_EFFECT_INTENTS = frozenset(
    {
        BossIntent.CHANGE,
        BossIntent.PAUSE,
        BossIntent.RESUME,
        BossIntent.STOP,
        BossIntent.APPROVE,
        BossIntent.REJECT,
        BossIntent.RESOLVE,
    }
)


def _match_text(message: str) -> str:
    """Normalize only the copy used for deterministic intent matching."""

    collapsed = " ".join(message.split()).casefold()
    return " ".join(collapsed.translate(_MATCH_PUNCTUATION).split())


def _read_only_intent(match_text: str) -> BossIntent | None:
    compact = match_text.replace(" ", "")
    for intent, patterns in _READ_ONLY_PATTERNS:
        if any(pattern.search(compact) for pattern in patterns):
            return intent
    return None


class DeterministicBossIntentRouter:
    """Recognize only high-precision Boss phrases without a model call."""

    def route(self, message: str, session: BossSession) -> RoutedIntent:
        request = message.strip()
        if request == "":
            return RoutedIntent(BossIntent.UNKNOWN, "clarify", 1.0, "empty input")
        normalized = " ".join(request.split())
        key = _match_text(normalized)
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
                "retry_task" if intent is BossIntent.RESOLVE else request
            )
            return RoutedIntent(intent, request)
        if request in {"?", "？"}:
            return RoutedIntent(BossIntent.HELP, request)
        read_only = _read_only_intent(key)
        if read_only is not None:
            return RoutedIntent(
                read_only,
                request,
                1.0,
                "deterministic read-only semantic pattern",
            )
        change = _CHANGE_PREFIX.fullmatch(normalized)
        if change is not None:
            detail = next(group for group in change.groups() if group is not None)
            if detail.strip():
                return RoutedIntent(
                    BossIntent.CHANGE,
                    request,
                    1.0,
                    "explicit change verb",
                )
        if key in {"现在怎么样", "how is it going", "how's it going"}:
            return RoutedIntent(BossIntent.QUERY_STATUS, request)
        return RoutedIntent(
            BossIntent.UNKNOWN,
            request,
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

    def __init__(
        self,
        client: SupervisorModelClient,
        *,
        minimum_confidence: float = 0.8,
        minimum_side_effect_confidence: float = 0.95,
    ):
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between zero and one")
        if not minimum_confidence <= minimum_side_effect_confidence <= 1.0:
            raise ValueError(
                "minimum_side_effect_confidence must be at least the general threshold"
            )
        self._client = client
        self._minimum_confidence = minimum_confidence
        self._minimum_side_effect_confidence = minimum_side_effect_confidence

    def route(self, message: str, session: BossSession) -> RoutedIntent:
        payload = self._client.create_structured_response(
            operation=SupervisorOperation.BOSS_ROUTING,
            system_prompt=(
                "Classify the Boss message into exactly one schema intent. "
                "You only route; never propose or perform a state change. "
                "Use UNKNOWN for ambiguous requests, especially possible state-changing "
                "requests. STOP means explicit whole-project cancellation; do not confuse "
                "it with PAUSE. Read-only examples: '还有几个任务' -> query_progress; "
                "'有什么需要我处理的吗' -> query_blockers; '接下来准备怎么做' -> "
                "query_plan; '现在忙什么' -> query_current_work. normalized_request "
                "must preserve the Boss meaning without adding facts. Never execute a "
                "command or output a state modification."
            ),
            user_prompt=f"Boss message:\n{message}",
            schema=boss_intent_schema(),
        )
        routed = parse_boss_intent(payload)
        threshold = (
            self._minimum_side_effect_confidence
            if routed.intent in _SIDE_EFFECT_INTENTS
            else self._minimum_confidence
        )
        if routed.confidence < threshold:
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
