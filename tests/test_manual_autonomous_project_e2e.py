import inspect
import unittest
from unittest.mock import patch

from scripts import manual_autonomous_project_e2e as manual


class ManualAutonomousProjectE2ETests(unittest.TestCase):
    def test_missing_key_stops_before_real_composition(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(manual.main(), 2)

    def test_correctness_e2e_preserves_transport_and_output_contract(self):
        config = manual._build_supervisor_config("deepseek-test")
        source = inspect.getsource(manual._build_compatibility_client)
        self.assertIsNone(config.max_output_tokens)
        self.assertIn("max_retries=0", source)
        self.assertIn("trust_env=False", source)
        self.assertNotIn("verify=False", source)

    def test_objective_requires_planning_instead_of_prebuilt_tasks(self):
        source = inspect.getsource(manual)
        self.assertIn("ProjectPlanningService", source)
        self.assertIn("AutonomousProjectService", source)
        self.assertNotIn("Requirement(", source)
        self.assertNotIn("Task(", source)


if __name__ == "__main__":
    unittest.main()
