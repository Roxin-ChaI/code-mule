import inspect
from pathlib import Path
import unittest

from scripts import local_execution_recovery_e2e


class ExecutionRecoveryE2EHarnessTests(unittest.TestCase):
    def test_harness_covers_all_recovery_scenarios_with_real_local_codex(self):
        source = inspect.getsource(local_execution_recovery_e2e)
        self.assertIn("_scenario_a", source)
        self.assertIn("_scenario_b", source)
        self.assertIn("_scenario_c", source)
        self.assertIn("ExecutionAlreadyOwned", source)
        self.assertIn("ExecutionRecoveryRequired", source)
        self.assertIn("RecoveryClassification.SESSION_RECOVERY_REQUIRED", source)
        self.assertIn("CodexWorkerSession", source)
        self.assertIn('(\"codex\", \"app-server\")', source)
        self.assertIn("TemporaryDirectory", source)
        self.assertNotIn("DEEPSEEK_API_KEY", source)

    def test_recovery_documentation_states_fail_closed_session_boundary(self):
        documentation = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "execution-recovery.md"
        ).read_text(encoding="utf-8")
        self.assertIn("PROJECT ALREADY RUNNING", documentation)
        self.assertIn("RECOVERY REQUIRED", documentation)
        self.assertIn("does **not** reconnect", documentation)
        self.assertIn("file existence alone never means", documentation)


if __name__ == "__main__":
    unittest.main()
