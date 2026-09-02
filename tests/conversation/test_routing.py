import unittest

from code_mule.conversation import (
    BossIntent,
    BossSession,
    CompositeBossIntentRouter,
    DeterministicBossIntentRouter,
    InvalidBossIntentResponse,
    StructuredBossIntentRouter,
    boss_intent_schema,
    parse_boss_intent,
)
from code_mule.supervisor.contracts import SupervisorOperation


class FakeClient:
    def __init__(self, payload): self.payload = payload; self.calls = []
    def create_structured_response(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


class FailingModel:
    def route(self, message, session):
        raise AssertionError("deterministic intent must not call model")


class BossRoutingTests(unittest.TestCase):
    def setUp(self): self.session = BossSession("project-1")

    def test_deterministic_status_plan_progress_blockers_and_controls(self):
        router = CompositeBossIntentRouter(model=FailingModel())
        cases = {
            "状态": BossIntent.QUERY_STATUS,
            "计划是什么？": BossIntent.QUERY_PLAN,
            "现在做到哪了？": BossIntent.QUERY_PROGRESS,
            "当前在做什么？": BossIntent.QUERY_CURRENT_WORK,
            "有什么问题？": BossIntent.QUERY_BLOCKERS,
            "增加 multiply": BossIntent.CHANGE,
            "先暂停": BossIntent.PAUSE,
            "继续": BossIntent.RESUME,
            "发生什么了？": BossIntent.INSPECT,
            "批准": BossIntent.APPROVE,
            "拒绝": BossIntent.REJECT,
            "重试": BossIntent.RESOLVE,
            "approve action-123": BossIntent.APPROVE,
            "拒绝 action-123": BossIntent.REJECT,
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertIs(router.route(message, self.session).intent, expected)

        self.assertEqual(
            router.route("approve action-123", self.session).normalized_request,
            "action-123",
        )

    def test_ambiguous_request_without_model_is_unknown(self):
        routed = CompositeBossIntentRouter().route("也许可以调整一下", self.session)
        self.assertIs(routed.intent, BossIntent.UNKNOWN)

    def test_schema_and_parser_are_strict(self):
        schema = boss_intent_schema()
        self.assertFalse(schema["additionalProperties"])
        valid = {
            "intent": "query_general",
            "normalized_request": "解释当前状态",
            "confidence": 0.9,
            "reason": "general query",
        }
        self.assertIs(parse_boss_intent(valid).intent, BossIntent.QUERY_GENERAL)
        with self.assertRaises(InvalidBossIntentResponse):
            parse_boss_intent({**valid, "state_change": "running"})
        with self.assertRaises(InvalidBossIntentResponse):
            parse_boss_intent({**valid, "confidence": "high"})

    def test_structured_model_router_uses_only_intent_schema(self):
        client = FakeClient(
            {
                "intent": "query_general",
                "normalized_request": "解释依赖关系",
                "confidence": 0.95,
                "reason": "question",
            }
        )
        routed = StructuredBossIntentRouter(client).route("解释一下", self.session)
        self.assertIs(routed.intent, BossIntent.QUERY_GENERAL)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertIs(call["operation"], SupervisorOperation.BOSS_ROUTING)
        self.assertEqual(call["schema"], boss_intent_schema())
        self.assertNotIn("project_state", call)

    def test_low_model_confidence_becomes_unknown(self):
        client = FakeClient(
            {
                "intent": "change",
                "normalized_request": "调整功能",
                "confidence": 0.4,
                "reason": "ambiguous",
            }
        )
        routed = StructuredBossIntentRouter(client).route("考虑改一下", self.session)
        self.assertIs(routed.intent, BossIntent.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
