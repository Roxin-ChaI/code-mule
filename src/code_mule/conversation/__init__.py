"""Boss conversational intent and bounded session contracts."""

from .contracts import (
    BossIntent,
    BossIntentRouter,
    BossSession,
    ConversationReply,
    InvalidBossIntentResponse,
    RoutedIntent,
)
from .routing import (
    CompositeBossIntentRouter,
    DeterministicBossIntentRouter,
    StructuredBossIntentRouter,
    boss_intent_schema,
    parse_boss_intent,
)
from .loop import run_chat_loop
from .service import BossCommandGateway, BossConversationService

__all__ = [
    "BossIntent",
    "BossIntentRouter",
    "BossCommandGateway",
    "BossConversationService",
    "BossSession",
    "CompositeBossIntentRouter",
    "ConversationReply",
    "DeterministicBossIntentRouter",
    "InvalidBossIntentResponse",
    "RoutedIntent",
    "StructuredBossIntentRouter",
    "boss_intent_schema",
    "parse_boss_intent",
    "run_chat_loop",
]
