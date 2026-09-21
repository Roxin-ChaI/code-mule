"""Real local Git/state/TaskCycle; deterministic fake Worker and Supervisor only."""

from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli import main
from code_mule.domain import HumanActionCategory, HumanResolutionStrategy, ProjectStatus, TaskStatus
from code_mule.human import HumanResolutionService, InvalidHumanResolution
from code_mule.progress import RecordingProgressSink
from code_mule.runtime import TaskCycleService, TaskCycleConfig, TaskCycleRequest, TaskPromptBuilder
from code_mule.state.store import JsonProjectStateStore
from code_mule.worker.parsing import build_execution_report
from code_mule.worker.structured_report import parse_structured_worker_report
from scripts.local_git_delivery_e2e import initial_state, FakeSupervisor, IdFactory
from .test_service import RepositoryCase, git, NOW


class ReportWorker:
    def __init__(self, root, status, required, mismatch=False, name="浏览器视觉验证"):
        self.root, self.status, self.required = root, status, required
        self.mismatch, self.name = mismatch, name
        self.calls = self.started = self.closed = 0
        self.thread_id = "fake-verification-thread"

    def start(self):
        self.started += 1

    def execute(self, request, *, report_id, created_at):
        self.calls += 1
        paths = ["README.md", "index.html", "css/style.css", "js/target-config.js", "js/render-target.js"]
        for name in paths:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("local fake Worker change\n")
        payload = {
            "status": "completed", "summary": "Local fake completed", "files_changed": paths[:-1] if self.mismatch else paths,
            "tests": [
                {"name": "focused tests", "status": "pass", "required": True, "detail": None},
                {"name": self.name, "status": self.status, "required": self.required, "detail": "secret-detail-DO-NOT-PERSIST"},
            ],
            "static_checks": [{"name": "syntax", "status": "pass", "required": True, "detail": None}],
            "git_state": "dirty", "issues": [], "human_action": None,
        }
        return build_execution_report(request, parse_structured_worker_report(json.dumps(payload)), report_id, created_at)

    def close(self):
        self.closed += 1


class VerificationLifecycleTests(RepositoryCase):
    def run_case(self, status, required, *, mismatch=False, name="浏览器视觉验证"):
        temporary = TemporaryDirectory(prefix="code-mule-verification-state-")
        self.addCleanup(temporary.cleanup)
        state_path = Path(temporary.name) / "project-state.json"
        store = JsonProjectStateStore(state_path)
        state = initial_state(self.root, rework=True)
        task = replace(state.tasks[0], status=TaskStatus.IN_PROGRESS)
        state = replace(state, project=replace(state.project, current_task_id=task.id), tasks=(task,))
        store.save(state)
        baseline = git(self.root, "rev-parse", "HEAD")
        worker = ReportWorker(self.root, status, required, mismatch, name)
        supervisor = FakeSupervisor(self.root, rework=False)
        progress = RecordingProgressSink()
        cycle = TaskCycleService(
            worker_session_factory=lambda: worker, supervisor=supervisor, store=store,
            clock=lambda: NOW, report_id_factory=IdFactory("report"),
            decision_id_factory=IdFactory("decision"), event_id_factory=IdFactory("event"),
            config=TaskCycleConfig(max_attempts=2), git_delivery=self.service, progress_sink=progress,
        )
        cycle.execute(TaskCycleRequest(task, TaskPromptBuilder().build(state, task)))
        final = store.load()
        self.assertEqual(worker.calls, 1)
        self.assertEqual(worker.started, 1)
        self.assertEqual(worker.closed, 1)
        self.assertEqual(final.tasks[0].execution_attempts, 1)
        self.assertNotIn("secret-detail-DO-NOT-PERSIST", state_path.read_text())
        self.assertNotIn("secret-detail-DO-NOT-PERSIST", repr(progress.events))
        if status in {"fail", "unknown"} or (required and status == "not_run"):
            if not mismatch:
                self.assertTrue(any(event.message == "Worker verification requires human action" for event in progress.events))
        self.assertFalse(any(command[:2] in (("git", "push"), ("git", "tag")) for command in self.commands))
        return final, supervisor, store, state_path, baseline

    def assert_blocked(self, status, required, *, mismatch=False, name="浏览器视觉验证"):
        state, supervisor, store, state_path, baseline = self.run_case(status, required, mismatch=mismatch, name=name)
        self.assertIs(state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertIs(state.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(supervisor.calls, 0)
        self.assertEqual(state.decisions, ())
        self.assertEqual(state.git_commit_results, ())
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), baseline)
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertTrue((self.root / "js/render-target.js").exists())
        self.assertFalse(any(command[:2] in (("git", "add"), ("git", "commit")) for command in self.commands))
        # A delivery-blocking report is stopped at the Worker verification
        # boundary before any Git delivery work begins.
        event = next(
            e
            for e in state.events
            if e.event_type in {"task.verification_blocked", "git.delivery_failed"}
        )
        action = state.human_actions[-1]
        self.assertEqual(action.status.value, "pending")
        if mismatch:
            self.assertEqual(event.metadata["error_type"], "GitOwnershipError")
            self.assertEqual(event.metadata["stage"], "ownership")
            self.assertEqual(
                event.metadata["failure_code"], "unrelated_worktree_changes"
            )
            self.assertEqual(event.metadata["baseline_head"], baseline)
            self.assertEqual(event.metadata["current_head"], baseline)
            self.assertIn("js/render-target.js", event.metadata["actual_paths"])
            self.assertNotIn("js/render-target.js", event.metadata["expected_paths"])
            self.assertEqual(event.metadata["staged_paths"], "[]")
            self.assertEqual(event.metadata["task_commit"], "-")
            self.assertEqual(event.metadata["ownership_status"], "mismatch")
            self.assertEqual(event.metadata["retry_safe"], "false")
            self.assertIs(action.category, HumanActionCategory.RECOVERY_UNCERTAIN)
            self.assertIn("repository ownership", action.requested_action)
            self.assertEqual(
                action.summary,
                "Repository contains changes outside the Worker-owned path set.",
            )
            before = state_path.read_bytes()
            for command in ("inspect", "diagnose"):
                out, err = StringIO(), StringIO()
                code = main(
                    [command, "--state-file", str(state_path), "--verbose"],
                    stdout=out,
                    stderr=err,
                )
                self.assertEqual(code, 0, err.getvalue())
                self.assertIn("Unrelated worktree changes", out.getvalue())
                self.assertIn("js/render-target.js", out.getvalue())
                self.assertIn("Retry safe", out.getvalue())
            self.assertEqual(state_path.read_bytes(), before)
        else:
            self.assertEqual(event.metadata["error_type"], "WorkerVerificationError")
            self.assertEqual(event.metadata["stage"], "verification")
            self.assertEqual(event.metadata["check_status"], status)
            self.assertEqual(event.metadata["check_type"], "test")
            self.assertEqual(event.metadata["check_required"], str(required).lower())
            self.assertIs(action.category, HumanActionCategory.WORKER_VERIFICATION)
            self.assertNotIn("ownership", action.requested_action)
            before = state_path.read_bytes()
            out, err = StringIO(), StringIO()
            code = main(["inspect", "--state-file", str(state_path), "--verbose"], stdout=out, stderr=err)
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(state_path.read_bytes(), before)
            self.assertIn("Worker verification", out.getvalue())
            self.assertIn("Failure stage verification", out.getvalue())
            self.assertIn("Required     " + ("yes" if required else "no"), out.getvalue())
            self.assertNotIn("secret-detail", out.getvalue())
            human = HumanResolutionService(store, clock=lambda: NOW, event_id_factory=IdFactory("human-event"), resolution_id_factory=IdFactory("resolution"))
            with self.assertRaises(InvalidHumanResolution):
                human.approve(action.id)
            with self.assertRaises(InvalidHumanResolution):
                human.resolve(action.id, HumanResolutionStrategy.RETRY_TASK)
            self.assertEqual(state_path.read_bytes(), before)
        return state

    def test_required_not_run(self):
        self.assert_blocked("not_run", True)

    def test_optional_not_run_reviews_and_creates_one_commit(self):
        state, supervisor, _, _, baseline = self.run_case("not_run", False)
        self.assertIs(state.tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(supervisor.calls, 1)
        self.assertEqual(len(state.decisions), 1)
        self.assertEqual(state.human_actions, ())
        self.assertEqual(len(state.git_commit_results), 1)
        self.assertEqual(git(self.root, "rev-parse", "HEAD^"), baseline)
        self.assertEqual(git(self.root, "status", "--short"), "")
        self.assertEqual(sum(command[:2] == ("git", "commit") for command in self.commands), 1)

    def test_required_fail(self):
        self.assert_blocked("fail", True)

    def test_optional_fail(self):
        self.assert_blocked("fail", False)

    def test_required_unknown(self):
        self.assert_blocked("unknown", True)

    def test_optional_unknown(self):
        self.assert_blocked("unknown", False)

    def test_path_mismatch_pass_checks(self):
        self.assert_blocked("pass", True, mismatch=True)

    def test_sensitive_name_and_detail_are_not_persisted_or_rendered(self):
        state = self.assert_blocked("not_run", True, name="DEEPSEEK_API_KEY=sk-private-fixture")
        self.assertEqual(state.execution_reports[0].verification_checks[1].name, "[check name withheld]")
        self.assertNotIn("sk-private-fixture", repr(state))


if __name__ == "__main__":
    unittest.main()
