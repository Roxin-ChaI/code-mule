import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GitDeliveryE2ETests(unittest.TestCase):
    def test_fake_supervisor_and_worker_cover_real_git_boundaries(self):
        completed = subprocess.run(
            (
                sys.executable,
                str(ROOT / "scripts" / "local_git_delivery_e2e.py"),
                "--fake-worker",
            ),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        two_tasks = payload["two_tasks"]
        self.assertEqual(two_tasks["project_status"], "done")
        self.assertEqual(two_tasks["task_statuses"], ["completed", "completed"])
        self.assertEqual(two_tasks["commit_count"], 2)
        self.assertEqual(
            two_tasks["commit_subjects"],
            ["feat(task): Add calculator addition", "feat(task): Add calculator tests"],
        )
        self.assertEqual(len(two_tasks["delivery_commits"]), 2)
        self.assertTrue(two_tasks["workspace_clean"])
        self.assertEqual(two_tasks["generated_tests"], "pass")
        self.assertEqual(two_tasks["final_review_decision"], "approve")
        self.assertTrue(
            all(check["status"] in {"pass", "skipped"} for check in two_tasks["verification_checks"])
        )
        rework = payload["rework"]
        self.assertEqual(rework["project_status"], "done")
        self.assertEqual(rework["commit_count"], 1)
        self.assertEqual(rework["review_commit_counts"], [1, 1])
        self.assertTrue(rework["workspace_clean"])


if __name__ == "__main__":
    unittest.main()
