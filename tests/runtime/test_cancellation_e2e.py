import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ProjectCancellationE2ETests(unittest.TestCase):
    def test_fake_worker_covers_all_cancellation_boundaries(self):
        completed = subprocess.run(
            (
                sys.executable,
                str(ROOT / "scripts" / "local_project_cancellation_e2e.py"),
                "--fake-worker",
            ),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        payload = json.loads(completed.stdout)
        active = payload["active"]
        self.assertEqual(active["project_status"], "cancelled")
        self.assertEqual(active["task_statuses"], ["completed", "cancelled"])
        self.assertEqual(active["task_ids_started"], ["TASK-ADD"])
        self.assertEqual(active["commit_count"], 1)
        self.assertEqual(active["delivery_commit_count"], 1)
        self.assertTrue(active["workspace_clean"])
        self.assertEqual(active["finalizer_calls"], 0)
        self.assertIn("project.cancel_requested", active["event_types"])
        self.assertIn("project.cancelled", active["event_types"])
        self.assertEqual(payload["idle"]["project_status"], "cancelled")
        self.assertEqual(payload["human_required"]["project_status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
