import unittest

from code_mule.conversation import (
    BossIntent,
    BossSession,
    ConversationReply,
    RoutedIntent,
)


class ConversationContractTests(unittest.TestCase):
    def test_intent_values_are_complete_and_stable(self):
        self.assertEqual(
            {item.name for item in BossIntent},
            {
                "QUERY_STATUS", "QUERY_PLAN", "QUERY_PROGRESS",
                "QUERY_CURRENT_WORK", "QUERY_BLOCKERS", "QUERY_GENERAL",
                "CHANGE", "PAUSE", "RESUME", "INSPECT", "APPROVE",
                "REJECT", "RESOLVE", "HELP", "UNKNOWN",
            },
        )

    def test_routed_intent_validation(self):
        routed = RoutedIntent(BossIntent.CHANGE, "增加 multiply", 0.9, "clear")
        self.assertIs(routed.intent, BossIntent.CHANGE)
        with self.assertRaises(ValueError):
            RoutedIntent(BossIntent.UNKNOWN, "", 1.0, "empty")
        with self.assertRaises(ValueError):
            RoutedIntent(BossIntent.UNKNOWN, "clarify", 1.1, "bad")

    def test_session_history_is_bounded_and_tracks_only_references(self):
        session = BossSession("project-1", max_history=2)
        for index in range(4):
            session.record(
                f"message-{index}",
                ConversationReply(
                    BossIntent.QUERY_STATUS,
                    (f"reply-{index}",),
                    referenced_task_id=f"T{index}",
                ),
            )
        self.assertEqual(session.recent_boss_messages, ("message-2", "message-3"))
        self.assertEqual(session.recent_assistant_replies, ("reply-2", "reply-3"))
        self.assertEqual(session.last_referenced_task_id, "T3")


if __name__ == "__main__":
    unittest.main()
