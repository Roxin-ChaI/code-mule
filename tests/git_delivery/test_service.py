import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from code_mule.domain.enums import TaskStatus
from code_mule.domain.models import ExecutionReport, Task
from code_mule.git_delivery import (
    DirtyGitBaseline,
    EmptyGitChangeSet,
    GitCommitError,
    GitDeliveryService,
    GitOwnershipError,
    UnexpectedGitHead,
)
from code_mule.git_delivery.service import run_git_command


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def task(title: str = "Add multiply support") -> Task:
    return Task(
        "TASK-1", "M1", title, "Implement it", TaskStatus.IN_PROGRESS,
        (), ("works",), 0, NOW, NOW, ("REQ-1",),
    )


def report(*paths: str) -> ExecutionReport:
    return ExecutionReport(
        "report-1", "TASK-1", 1, "completed", tuple(paths),
        ("unittest: pass",), ("compileall: pass",), "dirty", (), False,
        "implemented", NOW,
    )


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Code Mule Test")
        git(self.root, "config", "user.email", "code-mule@example.invalid")
        (self.root / "README.md").write_text("baseline\n", encoding="utf-8")
        git(self.root, "add", "--", "README.md")
        git(self.root, "commit", "-q", "-m", "initial")
        self.commands = []

        def recording_runner(arguments, cwd):
            self.commands.append(tuple(arguments))
            return run_git_command(arguments, cwd)

        self.service = GitDeliveryService(
            self.root, clock=lambda: NOW, runner=recording_runner
        )

    def tearDown(self):
        self.temporary.cleanup()


class GitDeliveryServiceTests(RepositoryCase):
    def test_clean_baseline_precise_stage_and_commit(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "calculator.py").write_text("def multiply(a, b): return a * b\n")
        changes = self.service.prepare_change_set(
            baseline, report("calculator.py"), ("calculator.py",)
        )
        result = self.service.commit(changes, task())
        self.assertEqual(result.baseline_head, baseline.baseline_head)
        self.assertEqual(result.changed_paths, ("calculator.py",))
        self.assertEqual(result.staged_paths, ("calculator.py",))
        self.assertEqual(git(self.root, "status", "--short"), "")
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "feat(task): Add multiply support")
        self.assertFalse(any(command[:2] == ("git", "push") for command in self.commands))
        self.assertFalse(any(command[:2] == ("git", "tag") for command in self.commands))
        self.assertNotIn(("git", "add", "."), self.commands)
        self.assertNotIn(("git", "add", "-A"), self.commands)

    def test_untracked_task_file_is_owned_and_committed(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "new.py").write_text("VALUE = 1\n")
        changes = self.service.prepare_change_set(
            baseline, report("new.py"), ("new.py",)
        )
        self.assertEqual(changes.untracked_paths, ("new.py",))
        result = self.service.commit(changes, task())
        self.assertEqual(result.changed_paths, ("new.py",))

    def test_dirty_baseline_fails_closed(self):
        (self.root / "README.md").write_text("external\n")
        with self.assertRaises(DirtyGitBaseline):
            self.service.capture_baseline("TASK-1")

    def test_unrelated_change_or_worker_staging_is_rejected(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        (self.root / "external.py").write_text("external\n")
        with self.assertRaises(GitOwnershipError):
            self.service.prepare_change_set(
                baseline, report("owned.py"), ("owned.py",)
            )
        git(self.root, "add", "--", "owned.py", "external.py")
        with self.assertRaises(GitOwnershipError):
            self.service.prepare_change_set(
                baseline,
                report("owned.py", "external.py"),
                ("owned.py", "external.py"),
            )

    def test_empty_change_and_failed_verification_are_rejected(self):
        baseline = self.service.capture_baseline("TASK-1")
        with self.assertRaises(EmptyGitChangeSet):
            self.service.prepare_change_set(baseline, report(), ())
        (self.root / "bad.py").write_text("bad\n")
        failed = replace(report("bad.py"), tests=("unittest: fail",))
        with self.assertRaises(GitOwnershipError):
            self.service.prepare_change_set(baseline, failed, ("bad.py",))

    def test_nonblocking_issues_are_left_for_supervisor_review(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        evidence = replace(
            report("owned.py"), issues=("Nonblocking environment note",)
        )
        changes = self.service.prepare_change_set(
            baseline, evidence, ("owned.py",)
        )
        self.assertEqual(changes.changed_paths, ("owned.py",))

    def test_unexpected_head_change_is_rejected(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "other.py").write_text("other\n")
        git(self.root, "add", "--", "other.py")
        git(self.root, "commit", "-q", "-m", "external")
        with self.assertRaises(UnexpectedGitHead):
            self.service.prepare_change_set(baseline, report("other.py"), ("other.py",))

    def test_commit_failure_is_typed_and_does_not_retry(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        changes = self.service.prepare_change_set(
            baseline, report("owned.py"), ("owned.py",)
        )
        original = self.service._runner
        commit_calls = 0

        def failing_commit(arguments, cwd):
            nonlocal commit_calls
            if tuple(arguments[:2]) == ("git", "commit"):
                commit_calls += 1
                return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "fail"})()
            return original(arguments, cwd)

        self.service._runner = failing_commit
        with self.assertRaises(GitCommitError):
            self.service.commit(changes, task())
        self.assertEqual(commit_calls, 1)


if __name__ == "__main__":
    unittest.main()
