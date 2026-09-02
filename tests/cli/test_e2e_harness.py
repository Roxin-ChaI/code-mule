import inspect
import unittest

from scripts import local_cli_e2e, manual_cli_e2e


class CliE2EHarnessTests(unittest.TestCase):
    def test_local_harness_uses_cli_composition_fake_supervisor_and_real_codex(self):
        source = inspect.getsource(local_cli_e2e)
        self.assertIn("cli_main", source)
        self.assertIn("ProductionCliComposition", source)
        self.assertIn("_FakeChangeSupervisor", source)
        self.assertIn('(\"codex\", \"app-server\")', source)
        self.assertNotIn("DEEPSEEK_API_KEY", source)
        self.assertIn('final.project.status is ProjectStatus.DONE', source)

    def test_manual_harness_uses_disposable_repo_and_explicit_change(self):
        source = inspect.getsource(manual_cli_e2e)
        self.assertIn("TemporaryDirectory", source)
        self.assertIn('"change", "Add multiply support"', source)
        self.assertIn('"change", "--apply"', source)
        self.assertIn("may incur billing", source)


if __name__ == "__main__":
    unittest.main()
