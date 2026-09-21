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
    GitDeliveryMode,
    GitDeliveryFailureCode,
    GitCommitError,
    GitDeliveryService,
    GitOwnershipError,
    UnexpectedGitHead,
    WorkerVerificationError,
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
        ("unittest: pass",), ("compileall: pass",), "dirty", (), None,
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
        with self.assertRaises(DirtyGitBaseline) as caught:
            self.service.capture_baseline("TASK-1")
        self.assertIs(
            caught.exception.details.failure_code,
            GitDeliveryFailureCode.BASELINE_MISMATCH,
        )
        self.assertEqual(caught.exception.details.actual_paths, ("README.md",))
        self.assertFalse(caught.exception.details.retry_safe)

    def test_partial_paths_are_measured_against_original_clean_baseline(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "partial.py").write_text("partial\n")
        self.assertEqual(
            self.service.capture_partial_paths(baseline), ("partial.py",)
        )
        git(self.root, "add", "--", "partial.py")
        with self.assertRaisesRegex(GitOwnershipError, "must not stage"):
            self.service.capture_partial_paths(baseline)

    def test_unrelated_change_or_worker_staging_is_rejected(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        (self.root / "external.py").write_text("external\n")
        with self.assertRaises(GitOwnershipError) as unrelated:
            self.service.prepare_change_set(
                baseline, report("owned.py"), ("owned.py",)
            )
        self.assertIs(
            unrelated.exception.details.failure_code,
            GitDeliveryFailureCode.UNRELATED_WORKTREE_CHANGES,
        )
        self.assertEqual(
            unrelated.exception.details.actual_paths, ("external.py", "owned.py")
        )
        git(self.root, "add", "--", "owned.py", "external.py")
        with self.assertRaises(GitOwnershipError) as staged:
            self.service.prepare_change_set(
                baseline,
                report("owned.py", "external.py"),
                ("owned.py", "external.py"),
            )
        self.assertIs(
            staged.exception.details.failure_code,
            GitDeliveryFailureCode.UNRELATED_STAGED_CHANGES,
        )
        self.assertEqual(
            staged.exception.details.staged_paths, ("external.py", "owned.py")
        )

    def test_missing_expected_path_has_typed_path_mismatch(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "actual.py").write_text("actual\n")
        with self.assertRaises(GitOwnershipError) as caught:
            self.service.prepare_change_set(
                baseline,
                report("actual.py", "missing.py"),
                ("actual.py", "missing.py"),
            )
        details = caught.exception.details
        self.assertIs(
            details.failure_code, GitDeliveryFailureCode.EXPECTED_PATHS_MISMATCH
        )
        self.assertEqual(details.expected_paths, ("actual.py", "missing.py"))
        self.assertEqual(details.actual_paths, ("actual.py",))

    def test_verified_empty_change_is_explicit_no_commit_delivery(self):
        baseline = self.service.capture_baseline("TASK-1")
        before = git(self.root, "rev-parse", "HEAD")

        changes = self.service.prepare_change_set(baseline, report(), ())
        result = self.service.verify_no_commit(changes, task())

        self.assertIs(changes.delivery_mode, GitDeliveryMode.NO_COMMIT_REQUIRED)
        self.assertEqual(result.verified_head, before)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.root, "status", "--short"), "")
        self.assertFalse(any(command[:2] == ("git", "commit") for command in self.commands))

    def test_empty_change_still_requires_passing_verification(self):
        baseline = self.service.capture_baseline("TASK-1")
        failed_empty = replace(report(), tests=("unittest: fail",))
        with self.assertRaises(WorkerVerificationError):
            self.service.prepare_change_set(baseline, failed_empty, ())

    def test_report_and_repository_path_mismatches_never_become_noop(self):
        baseline = self.service.capture_baseline("TASK-1")
        with self.assertRaises(GitOwnershipError):
            self.service.prepare_change_set(baseline, report("claimed.py"), ("claimed.py",))
        (self.root / "actual.py").write_text("changed\n")
        with self.assertRaises(GitOwnershipError):
            self.service.prepare_change_set(baseline, report(), ())
        git(self.root, "add", "--", "actual.py")
        with self.assertRaises(GitOwnershipError):
            self.service.prepare_change_set(baseline, report(), ())

    def test_changed_delivery_cannot_use_no_commit_boundary(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "changed.py").write_text("changed\n")
        changes = self.service.prepare_change_set(
            baseline, report("changed.py"), ("changed.py",)
        )
        self.assertIs(changes.delivery_mode, GitDeliveryMode.COMMIT_REQUIRED)
        with self.assertRaises(GitOwnershipError):
            self.service.verify_no_commit(changes, task())

    def test_changed_and_failed_verification_is_rejected(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "bad.py").write_text("bad\n")
        failed = replace(report("bad.py"), tests=("unittest: fail",))
        with self.assertRaises(WorkerVerificationError):
            self.service.prepare_change_set(baseline, failed, ("bad.py",))

    def test_required_not_run_is_verification_not_ownership(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        evidence = replace(report("owned.py"), tests=("visual: not_run",))
        with self.assertRaises(WorkerVerificationError) as caught:
            self.service.prepare_change_set(baseline, evidence, ("owned.py",))
        self.assertNotIsInstance(caught.exception, GitOwnershipError)

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
        with self.assertRaises(UnexpectedGitHead) as caught:
            self.service.prepare_change_set(baseline, report("other.py"), ("other.py",))
        details = caught.exception.details
        self.assertIs(
            details.failure_code, GitDeliveryFailureCode.UNEXPECTED_HEAD_CHANGE
        )
        self.assertEqual(details.baseline_head, baseline.baseline_head)
        self.assertEqual(details.current_head, git(self.root, "rev-parse", "HEAD"))
        self.assertIsNone(details.task_commit)

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
        with self.assertRaises(GitCommitError) as caught:
            self.service.commit(changes, task())
        self.assertEqual(commit_calls, 1)
        self.assertIs(
            caught.exception.details.failure_code,
            GitDeliveryFailureCode.COMMIT_CREATION_FAILED,
        )
        self.assertFalse(caught.exception.details.retry_safe)

    def test_commit_success_without_new_head_is_commit_missing(self):
        baseline = self.service.capture_baseline("TASK-1")
        (self.root / "owned.py").write_text("owned\n")
        changes = self.service.prepare_change_set(
            baseline, report("owned.py"), ("owned.py",)
        )
        original = self.service._runner

        def false_success(arguments, cwd):
            if tuple(arguments[:2]) == ("git", "commit"):
                return type(
                    "Result", (), {"returncode": 0, "stdout": "", "stderr": ""}
                )()
            return original(arguments, cwd)

        self.service._runner = false_success
        with self.assertRaises(GitCommitError) as caught:
            self.service.commit(changes, task())
        self.assertIs(
            caught.exception.details.failure_code,
            GitDeliveryFailureCode.COMMIT_MISSING,
        )

    def test_previous_task_commit_is_current_task_baseline(self):
        first = self.service.capture_baseline("TASK-1")
        (self.root / "first.py").write_text("first\n")
        first_result = self.service.commit(
            self.service.prepare_change_set(
                first, report("first.py"), ("first.py",)
            ),
            task("Complete first task"),
        )
        second = self.service.capture_baseline("TASK-2")
        self.assertEqual(second.baseline_head, first_result.commit_sha)
        (self.root / "second.py").write_text("second\n")
        second_report = replace(
            report("second.py"), id="report-2", task_id="TASK-2"
        )
        second_task = replace(task("Complete second task"), id="TASK-2")
        second_result = self.service.commit(
            self.service.prepare_change_set(
                second, second_report, ("second.py",)
            ),
            second_task,
        )
        self.assertEqual(
            git(self.root, "rev-parse", f"{second_result.commit_sha}^"),
            first_result.commit_sha,
        )

    def test_rc_rename_source_and_destination_form_one_owned_commit(self):
        (self.root / "app.py").write_text("def main(): pass\n")
        git(self.root, "add", "--", "app.py")
        git(self.root, "commit", "-q", "-m", "feat: add app")
        baseline = self.service.capture_baseline("TASK-1")
        destination = self.root / "code_mule_service" / "service.py"
        destination.parent.mkdir()
        (self.root / "app.py").rename(destination)
        changes = self.service.prepare_change_set(
            baseline,
            report("app.py", "code_mule_service/service.py"),
            ("app.py", "code_mule_service/service.py"),
        )
        result = self.service.commit(changes, task("Move application service"))
        self.assertEqual(
            result.staged_paths, ("app.py", "code_mule_service/service.py")
        )
        self.assertEqual(git(self.root, "status", "--short"), "")
        self.assertIn(
            "app.py => code_mule_service/service.py",
            git(self.root, "show", "--stat", "--oneline", "HEAD"),
        )


if __name__ == "__main__":
    unittest.main()
