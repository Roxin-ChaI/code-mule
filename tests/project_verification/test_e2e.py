import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ProjectVerificationE2ETests(unittest.TestCase):
    def test_real_git_checks_and_fake_final_supervisor_cover_completion_gates(self):
        completed = subprocess.run(
            (
                sys.executable,
                str(ROOT / "scripts" / "local_project_verification_e2e.py"),
            ),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        success = payload["success"]
        self.assertEqual(success["project_status"], "done")
        self.assertEqual(success["task_statuses"], ["completed", "completed"])
        self.assertEqual(success["commit_count"], 2)
        self.assertTrue(success["workspace_clean"])
        for scenario, expected_check in (
            ("test_failure", "project tests"),
            ("dirty_git", "Git clean"),
        ):
            with self.subTest(scenario=scenario):
                result = payload[scenario]
                self.assertEqual(result["project_status"], "human_required")
                self.assertIn(expected_check, result["failed_checks"])
                self.assertEqual(result["final_supervisor_calls"], 0)
                self.assertTrue(result["result_persisted"])


if __name__ == "__main__":
    unittest.main()
