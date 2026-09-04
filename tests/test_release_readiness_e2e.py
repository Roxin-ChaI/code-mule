import inspect
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from code_mule.cli import DEFAULT_STATE_FILE, build_parser
from scripts import manual_release_e2e


ROOT = Path(__file__).resolve().parents[1]


class ReleaseReadinessE2ETests(unittest.TestCase):
    def test_full_local_change_delivery_finalization_and_cancellation(self):
        completed = subprocess.run(
            (
                sys.executable,
                str(ROOT / "scripts" / "local_release_readiness_e2e.py"),
                "--fake-worker",
            ),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        release = payload["release"]
        cancellation = payload["cancellation"]
        self.assertEqual(release["safe_point_status"], "change_requested")
        self.assertEqual(release["replacement_version"], 2)
        self.assertEqual(release["final_status"], "done")
        self.assertEqual(release["delivery_commit_count"], 4)
        self.assertEqual(release["repository_commit_count"], 4)
        self.assertEqual(release["session_count"], release["unique_session_count"])
        self.assertTrue(release["verification_passed"])
        self.assertTrue(release["workspace_clean"])
        self.assertEqual(cancellation["project_status"], "cancelled")
        self.assertEqual(cancellation["task_ids_started"], ["TASK-ADD"])
        self.assertEqual(cancellation["finalizer_calls"], 0)

    def test_formal_cli_surface_and_default_project_discovery_path(self):
        commands = {
            "init",
            "run",
            "status",
            "chat",
            "ask",
            "change",
            "pause",
            "resume",
            "stop",
            "inspect",
            "approve",
            "reject",
            "resolve",
        }
        parser = build_parser()
        for command in commands:
            arguments = [command]
            if command == "init":
                arguments += ["--project-id", "P", "--name", "Project"]
            elif command == "ask":
                arguments += ["status?"]
            elif command == "change":
                arguments += ["add feature"]
            elif command in {"approve", "reject"}:
                arguments += ["action-1"]
            elif command == "resolve":
                arguments += ["action-1", "--strategy", "acknowledge"]
            parsed = parser.parse_args(arguments)
            self.assertEqual(parsed.command, command)
            self.assertEqual(parsed.state_file, DEFAULT_STATE_FILE)
        self.assertEqual(DEFAULT_STATE_FILE, Path(".code-mule/project-state.json"))

    def test_manual_release_path_is_authenticated_boss_only(self):
        source = inspect.getsource(manual_release_e2e)
        self.assertIn("TemporaryDirectory", inspect.getsource(
            __import__(
                "scripts.local_release_readiness_e2e",
                fromlist=["run_release_scenario"],
            )
        ))
        self.assertIn("DeepSeekSupervisorModelClient", source)
        self.assertIn("real_worker=True", source)
        self.assertIn("max_retries=0", source)
        self.assertIn("trust_env=False", source)
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(
            StringIO()
        ), redirect_stdout(StringIO()):
            self.assertEqual(manual_release_e2e.main(), 2)


if __name__ == "__main__":
    unittest.main()
