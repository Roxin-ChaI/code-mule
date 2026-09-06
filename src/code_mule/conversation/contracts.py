"""Typed contracts for Boss conversation without project-state authority."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class BossIntent(StrEnum):
    QUERY_STATUS = "query_status"
    QUERY_PLAN = "query_plan"
    QUERY_PROGRESS = "query_progress"
    QUERY_CURRENT_WORK = "query_current_work"
    QUERY_BLOCKERS = "query_blockers"
    QUERY_GENERAL = "query_general"
    DIAGNOSE = "diagnose"
    CHANGE = "change"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    INSPECT = "inspect"
    APPROVE = "approve"
    REJECT = "reject"
    RESOLVE = "resolve"
    HELP = "help"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RoutedIntent:
    intent: BossIntent
    normalized_request: str
    confidence: float = 1.0
    reason: str = "deterministic"

    def __post_init__(self) -> None:
        if self.normalized_request == "":
            raise ValueError("normalized_request must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        if self.reason == "":
            raise ValueError("reason must not be empty")


@dataclass(frozen=True)
class ConversationReply:
    intent: BossIntent
    lines: tuple[str, ...]
    referenced_task_id: str | None = None
    referenced_action_id: str | None = None
    referenced_change_id: str | None = None

    def __post_init__(self) -> None:
        if not self.lines or any(line is None for line in self.lines):
            raise ValueError("conversation reply requires display lines")


class InvalidBossIntentResponse(ValueError):
    """Raised when a model router violates the strict intent contract."""


class BossIntentRouter(Protocol):
    def route(self, message: str, session: "BossSession") -> RoutedIntent: ...


@dataclass
class BossSession:
    project_id: str
    max_history: int = 8
    recent_boss_messages: tuple[str, ...] = ()
    recent_assistant_replies: tuple[str, ...] = ()
    last_referenced_action_id: str | None = None
    last_referenced_change_id: str | None = None
    last_referenced_task_id: str | None = None

    def __post_init__(self) -> None:
        if self.project_id == "":
            raise ValueError("project_id must not be empty")
        if self.max_history < 1:
            raise ValueError("max_history must be positive")
        self.recent_boss_messages = self.recent_boss_messages[-self.max_history :]
        self.recent_assistant_replies = self.recent_assistant_replies[
            -self.max_history :
        ]

    def record(self, message: str, reply: ConversationReply) -> None:
        if message == "":
            raise ValueError("message must not be empty")
        self.recent_boss_messages = (
            self.recent_boss_messages + (message,)
        )[-self.max_history :]
        rendered = "\n".join(reply.lines)
        self.recent_assistant_replies = (
            self.recent_assistant_replies + (rendered,)
        )[-self.max_history :]
        if reply.referenced_action_id is not None:
            self.last_referenced_action_id = reply.referenced_action_id
        if reply.referenced_change_id is not None:
            self.last_referenced_change_id = reply.referenced_change_id
        if reply.referenced_task_id is not None:
            self.last_referenced_task_id = reply.referenced_task_id


__all__ = [
    "BossIntent",
    "BossIntentRouter",
    "BossSession",
    "ConversationReply",
    "InvalidBossIntentResponse",
    "RoutedIntent",
]
