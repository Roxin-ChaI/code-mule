import inspect
import unittest
from unittest.mock import patch

from scripts import manual_boss_chat_e2e as manual


class ManualBossChatE2ETests(unittest.TestCase):
    def test_missing_key_stops_before_real_routing(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(manual.main(), 2)

    def test_manual_chat_is_disposable_boss_only_and_explicitly_billed(self):
        source = inspect.getsource(manual)
        self.assertIn("TemporaryDirectory", source)
        self.assertIn("REAL DEEPSEEK BOSS CHAT — MANUAL ONLY", source)
        self.assertIn("may incur billing", source)
        self.assertIn('"chat"', source)
        self.assertIn("计划是什么？", source)
        self.assertIn("增加 export CSV", source)
        self.assertIn("发生什么了？", source)
        self.assertNotIn("CodexWorkerService", source)
        self.assertNotIn("app-server", source)
        self.assertNotIn("change --apply", source)


if __name__ == "__main__":
    unittest.main()
