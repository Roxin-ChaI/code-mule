import inspect
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from code_mule.cli import DEFAULT_STATE_FILE, build_parser
from code_mule.progress import ProgressEvent, ProgressEventType, RecordingProgressSink
from code_mule.worker import CodexTurnTimeout
from scripts import manual_release_e2e
from scripts.local_release_readiness_e2e import (
    ReleaseInterruption,
    ReleaseScenarioInterrupted,
    run_release_scenario,
)


ROOT = Path(__file__).resolve().parents[1]


class _FailingReleaseSession:
    instances = []
    failure = CodexTurnTimeout("deadline exceeded")

    def __init__(self, _repository, *, progress=None):
        self._thread_id = f"test-thread-{len(self.instances) + 1}"
        self.closed = False
        self.progress = progress
        self.instances.append(self)

    @property
    def thread_id(self):
        return self._thread_id

    def start(self):
        return None

    def execute(self, *_args, **_kwargs):
        if self.progress is not None:
            self.progress.emit(
                ProgressEvent(
                    ProgressEventType.WORKER_ACTIVITY,
                    datetime.now(UTC),
                    None,
                    None,
                    None,
                    "Inspecting repository",
                    {"activity": "search"},
                )
            )
        raise self.failure

    def close(self):
        self.closed = True


class _InterruptedReleaseSession(_FailingReleaseSession):
    instances = []
    failure = KeyboardInterrupt()


class ReleaseReadinessE2ETests(unittest.TestCase):
    def setUp(self):
        _FailingReleaseSession.instances.clear()
        _InterruptedReleaseSession.instances.clear()

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

    def test_stalled_worker_is_bounded_and_not_dispatched_twice(self):
        progress = RecordingProgressSink()
        result = run_release_scenario(
            real_worker=False,
            progress_sink=progress,
            worker_timeout_seconds=0.01,
            worker_session_factory=lambda repository: _FailingReleaseSession(
                repository, progress=progress
            ),
        )

        self.assertEqual(result["final_status"], "human_required")
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(result["pending_action_categories"], ["recovery_uncertain"])
        self.assertEqual(result["lease_statuses"], ["released"])
        self.assertTrue(_FailingReleaseSession.instances[0].closed)
        self.assertIn(
            ProgressEventType.WORKER_ACTIVITY,
            tuple(event.type for event in progress.events),
        )
        projected = repr(progress.events)
        self.assertNotIn("prompt", projected.lower())
        self.assertNotIn("secret", projected.lower())

    def test_active_worker_interrupt_persists_typed_recovery_and_closes(self):
        with self.assertRaises(ReleaseScenarioInterrupted) as raised:
            run_release_scenario(
                real_worker=False,
                worker_session_factory=_InterruptedReleaseSession,
            )

        details = raised.exception.details
        self.assertEqual(details.stage, "worker")
        self.assertIsNotNone(details.task_id)
        self.assertEqual(details.project_status, "human_required")
        self.assertTrue(details.recovery_required)
        self.assertTrue(details.sessions_closed)
        self.assertEqual(details.lease_statuses, ("released",))
        self.assertTrue(_InterruptedReleaseSession.instances[0].closed)

    def test_manual_interrupt_is_concise_without_traceback(self):
        interruption = ReleaseScenarioInterrupted(
            ReleaseInterruption(
                "worker",
                "TASK-1",
                "human_required",
                True,
                True,
                ("released",),
            )
        )
        stderr = StringIO()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-only"}, clear=True), patch(
            "scripts.manual_release_e2e.OpenAI", return_value=object()
        ), patch(
            "scripts.manual_release_e2e.DefaultHttpx2Client", return_value=object()
        ), patch(
            "scripts.manual_release_e2e.DeepSeekSupervisorModelClient", return_value=object()
        ), patch(
            "scripts.manual_release_e2e.SupervisorService", return_value=object()
        ), patch(
            "scripts.manual_release_e2e.run_release_scenario",
            side_effect=interruption,
        ), redirect_stderr(stderr), redirect_stdout(StringIO()):
            exit_code = manual_release_e2e.main([])

        output = stderr.getvalue()
        self.assertEqual(exit_code, 130)
        self.assertIn("MANUAL E2E INTERRUPTED", output)
        self.assertIn("Stage: worker", output)
        self.assertIn("Project state: HUMAN_REQUIRED", output)
        self.assertIn("Recovery required: yes", output)
        self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()
