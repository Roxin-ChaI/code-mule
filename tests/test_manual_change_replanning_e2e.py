import inspect
import unittest
from unittest.mock import patch

from scripts import manual_change_replanning_e2e as manual


class ManualChangeReplanningE2ETests(unittest.TestCase):
    def test_missing_key_stops_before_real_composition(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(manual.main(), 2)

    def test_transport_keeps_tls_defaults_and_no_output_cap(self):
        config = manual._build_supervisor_config("deepseek-test")
        source = inspect.getsource(manual._build_compatibility_client)
        self.assertIsNone(config.max_output_tokens)
        self.assertIn("max_retries=0", source)
        self.assertIn("trust_env=False", source)
        self.assertNotIn("verify=False", source)

    def test_manual_path_uses_real_planning_replanning_and_disposable_repo(self):
        source = inspect.getsource(manual)
        self.assertIn(
            "REAL DEEPSEEK + CODEX CHANGE REPLANNING — MANUAL ONLY",
            source,
        )
        self.assertIn("ProjectPlanningService", source)
        self.assertIn("ChangeReplanningService", source)
        self.assertIn("ChangeExecutionService", source)
        self.assertIn("TemporaryDirectory", source)
        self.assertNotIn("Requirement(", source)
        self.assertNotIn("Task(", source)


if __name__ == "__main__":
    unittest.main()
