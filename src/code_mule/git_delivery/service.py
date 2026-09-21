"""Deterministic, path-scoped local Git delivery service."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import subprocess
from typing import Protocol

from code_mule.domain.models import ExecutionReport, Task
from code_mule.domain.worker_verification import (
    blocking_checks,
    evidence_matches_text,
    legacy_checks,
)

from .contracts import (
    DirtyGitBaseline,
    GitBaseline,
    GitChangeSet,
    GitCommitError,
    GitCommitResult,
    GitDeliveryFailureCode,
    GitDeliveryFailureDetails,
    GitDeliveryMode,
    GitNoCommitResult,
    GitOwnershipError,
    GitOwnershipStatus,
    GitStagingError,
    UnexpectedGitHead,
    WorkerVerificationError,
)


@dataclass(frozen=True)
class GitCommandResult:
    returncode: int
    stdout: str
    stderr: str


class GitCommandRunner(Protocol):
    def __call__(self, arguments: Sequence[str], cwd: Path) -> GitCommandResult: ...


def run_git_command(arguments: Sequence[str], cwd: Path) -> GitCommandResult:
    try:
        completed = subprocess.run(
            tuple(arguments),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return GitCommandResult(127, "", "Git command could not be executed")
    return GitCommandResult(completed.returncode, completed.stdout, completed.stderr)


class GitDeliveryService:
    """Own one clean-baseline-to-commit transaction for a Task."""

    def __init__(
        self,
        workspace: Path,
        *,
        clock: Callable[[], datetime],
        runner: GitCommandRunner = run_git_command,
    ) -> None:
        self._workspace = workspace.resolve()
        self._clock = clock
        self._runner = runner

    def capture_baseline(self, task_id: str) -> GitBaseline:
        root = Path(self._required(("git", "rev-parse", "--show-toplevel")).strip()).resolve()
        if not self._workspace.is_relative_to(root):
            raise GitOwnershipError(
                "workspace is outside the discovered repository",
                details=self._failure(
                    GitDeliveryFailureCode.OWNERSHIP_UNCERTAIN,
                    ownership=GitOwnershipStatus.UNCERTAIN,
                    summary="Workspace is outside the discovered Git repository.",
                ),
            )
        head = self._required(("git", "rev-parse", "HEAD"), cwd=root).strip()
        status = self._required(
            ("git", "status", "--short", "--untracked-files=all"), cwd=root
        )
        entries = tuple(line for line in status.splitlines() if line)
        if entries:
            changed, _, staged = self._status_paths(root)
            error = DirtyGitBaseline(
                "repository must be clean before Task dispatch",
                details=self._failure(
                    GitDeliveryFailureCode.BASELINE_MISMATCH,
                    baseline_head=head,
                    current_head=head,
                    actual=changed,
                    staged=staged,
                    ownership=GitOwnershipStatus.NOT_VERIFIED,
                    summary="Task baseline is not a clean Git workspace.",
                ),
            )
            error.status_entries = entries
            raise error
        return GitBaseline(task_id, str(root), head, entries)

    def prepare_change_set(
        self,
        baseline: GitBaseline,
        report: ExecutionReport,
        owned_paths: tuple[str, ...],
    ) -> GitChangeSet:
        root = Path(baseline.repository_root)
        self._assert_root(root)
        self._assert_head(root, baseline.baseline_head)
        self._required(("git", "diff", "--check"), cwd=root)
        changed, untracked, staged = self._status_paths(root)
        expected = self._normalize_owned_paths(owned_paths)
        if set(changed) != set(expected):
            code = (
                GitDeliveryFailureCode.UNRELATED_WORKTREE_CHANGES
                if set(changed) - set(expected)
                else GitDeliveryFailureCode.EXPECTED_PATHS_MISMATCH
            )
            raise GitOwnershipError(
                "repository changes do not exactly match Worker-owned paths",
                details=self._failure(
                    code,
                    baseline_head=baseline.baseline_head,
                    current_head=baseline.baseline_head,
                    expected=expected,
                    actual=changed,
                    staged=staged,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary=(
                        "Repository contains changes outside the Worker-owned path set."
                        if code is GitDeliveryFailureCode.UNRELATED_WORKTREE_CHANGES
                        else "Worker-owned paths do not match the repository change set."
                    ),
                ),
            )
        if staged:
            raise GitOwnershipError(
                "Worker must not stage paths; staging belongs to the Orchestrator",
                details=self._failure(
                    GitDeliveryFailureCode.UNRELATED_STAGED_CHANGES,
                    baseline_head=baseline.baseline_head,
                    current_head=baseline.baseline_head,
                    expected=expected,
                    actual=changed,
                    staged=staged,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Paths were staged outside the Orchestrator delivery boundary.",
                ),
            )
        # Only classify a cleanly attributable, unchanged-baseline delivery as
        # a verification block. Unknown ownership always retains its safe gate.
        self._validate_report(report)
        return GitChangeSet(
            task_id=baseline.task_id,
            repository_root=str(root),
            baseline_head=baseline.baseline_head,
            changed_paths=changed,
            untracked_paths=untracked,
            staged_paths=(),
        )

    def verify_no_commit(
        self, change_set: GitChangeSet, task: Task
    ) -> GitNoCommitResult:
        """Revalidate one approved zero-diff delivery without creating a commit."""

        if change_set.task_id != task.id:
            raise GitOwnershipError(
                "no-commit delivery targets a different Task",
                details=self._failure(
                    GitDeliveryFailureCode.OWNERSHIP_UNCERTAIN,
                    baseline_head=change_set.baseline_head,
                    expected=change_set.changed_paths,
                    ownership=GitOwnershipStatus.UNCERTAIN,
                    summary="No-change delivery references a different Task.",
                ),
            )
        if change_set.delivery_mode is not GitDeliveryMode.NO_COMMIT_REQUIRED:
            raise GitOwnershipError("changed Task still requires a commit")
        root = Path(change_set.repository_root)
        self._assert_root(root)
        self._assert_head(root, change_set.baseline_head)
        changed, _, staged = self._status_paths(root)
        if changed or staged:
            raise GitOwnershipError(
                "repository changed after zero-diff ownership validation",
                details=self._failure(
                    GitDeliveryFailureCode.UNRELATED_WORKTREE_CHANGES,
                    baseline_head=change_set.baseline_head,
                    current_head=change_set.baseline_head,
                    actual=changed,
                    staged=staged,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Repository changed after no-change ownership validation.",
                ),
            )
        self._required(("git", "diff", "--check"), cwd=root)
        return GitNoCommitResult(
            task.id,
            str(root),
            change_set.baseline_head,
            change_set.baseline_head,
            self._clock(),
        )

    def capture_partial_paths(self, baseline: GitBaseline) -> tuple[str, ...]:
        """Verify and describe unfinished Worker edits against its clean baseline."""

        root = Path(baseline.repository_root)
        self._assert_root(root)
        self._assert_head(root, baseline.baseline_head)
        self._required(("git", "diff", "--check"), cwd=root)
        changed, _, staged = self._status_paths(root)
        if staged:
            raise GitOwnershipError(
                "Worker must not stage paths; staging belongs to the Orchestrator",
                details=self._failure(
                    GitDeliveryFailureCode.UNRELATED_STAGED_CHANGES,
                    baseline_head=baseline.baseline_head,
                    current_head=baseline.baseline_head,
                    actual=changed,
                    staged=staged,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Partial Worker changes include paths staged outside delivery.",
                ),
            )
        return changed

    def commit(self, change_set: GitChangeSet, task: Task) -> GitCommitResult:
        root = Path(change_set.repository_root)
        self._assert_root(root)
        self._assert_head(root, change_set.baseline_head)
        current, _, staged_before = self._status_paths(root)
        if current != change_set.changed_paths:
            code = (
                GitDeliveryFailureCode.UNRELATED_WORKTREE_CHANGES
                if set(current) - set(change_set.changed_paths)
                else GitDeliveryFailureCode.EXPECTED_PATHS_MISMATCH
            )
            raise GitOwnershipError(
                "repository changed after ownership validation",
                details=self._failure(
                    code,
                    baseline_head=change_set.baseline_head,
                    current_head=change_set.baseline_head,
                    expected=change_set.changed_paths,
                    actual=current,
                    staged=staged_before,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Repository paths changed after ownership validation.",
                ),
            )
        if staged_before:
            raise GitOwnershipError(
                "repository changed after ownership validation",
                details=self._failure(
                    GitDeliveryFailureCode.UNRELATED_STAGED_CHANGES,
                    baseline_head=change_set.baseline_head,
                    current_head=change_set.baseline_head,
                    expected=change_set.changed_paths,
                    actual=current,
                    staged=staged_before,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Paths were staged after ownership validation.",
                ),
            )

        stage = self._run(("git", "add", "--", *change_set.changed_paths), root)
        if stage.returncode != 0:
            raise GitStagingError(
                "precise Git staging failed",
                details=self._failure(
                    GitDeliveryFailureCode.STAGING_FAILED,
                    baseline_head=change_set.baseline_head,
                    current_head=change_set.baseline_head,
                    expected=change_set.changed_paths,
                    actual=current,
                    staged=staged_before,
                    ownership=GitOwnershipStatus.VERIFIED,
                    summary="Precise staging of Worker-owned paths failed.",
                ),
            )
        cached_check = self._run(("git", "diff", "--cached", "--check"), root)
        if cached_check.returncode != 0:
            _, _, staged_after_failure = self._status_paths(root)
            raise GitStagingError(
                "staged Task change fails git diff --check",
                details=self._failure(
                    GitDeliveryFailureCode.STAGING_FAILED,
                    baseline_head=change_set.baseline_head,
                    current_head=change_set.baseline_head,
                    expected=change_set.changed_paths,
                    actual=current,
                    staged=staged_after_failure,
                    ownership=GitOwnershipStatus.VERIFIED,
                    summary="Staged Worker changes failed Git whitespace validation.",
                ),
            )
        current_after_stage, _, staged_after_stage = self._status_paths(root)
        if (
            current_after_stage != change_set.changed_paths
            or staged_after_stage != change_set.changed_paths
        ):
            raise GitOwnershipError(
                "repository changed during precise staging",
                details=self._failure(
                    (
                        GitDeliveryFailureCode.UNRELATED_STAGED_CHANGES
                        if set(staged_after_stage) - set(change_set.changed_paths)
                        else GitDeliveryFailureCode.EXPECTED_PATHS_MISMATCH
                    ),
                    baseline_head=change_set.baseline_head,
                    current_head=change_set.baseline_head,
                    expected=change_set.changed_paths,
                    actual=current_after_stage,
                    staged=staged_after_stage,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Precisely staged paths do not match the verified change set.",
                ),
            )
        # Porcelain status is authoritative because it preserves both sides of
        # an index rename. `git diff --cached --name-only` only reports the
        # destination and would falsely reject a valid source+destination set.
        staged = staged_after_stage
        unstaged_check = self._run(("git", "diff", "--quiet", "--"), root)
        if unstaged_check.returncode != 0:
            raise GitOwnershipError(
                "repository changed after Task paths were staged",
                details=self._failure(
                    GitDeliveryFailureCode.UNRELATED_WORKTREE_CHANGES,
                    baseline_head=change_set.baseline_head,
                    current_head=change_set.baseline_head,
                    expected=change_set.changed_paths,
                    actual=current_after_stage,
                    staged=staged_after_stage,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Unstaged changes appeared during precise Task delivery.",
                ),
            )
        self._assert_head(root, change_set.baseline_head)

        message = commit_message(task)
        committed = self._run(("git", "commit", "-m", message), root)
        if committed.returncode != 0:
            current_head = self._head(root)
            raise GitCommitError(
                "Git commit failed",
                details=self._failure(
                    GitDeliveryFailureCode.COMMIT_CREATION_FAILED,
                    baseline_head=change_set.baseline_head,
                    current_head=current_head,
                    expected=change_set.changed_paths,
                    actual=current_after_stage,
                    staged=staged_after_stage,
                    task_commit=(
                        current_head
                        if current_head is not None
                        and current_head != change_set.baseline_head
                        else None
                    ),
                    ownership=GitOwnershipStatus.VERIFIED,
                    summary="Local Task commit could not be created.",
                ),
            )
        commit_sha = self._required(("git", "rev-parse", "HEAD"), cwd=root).strip()
        if commit_sha == change_set.baseline_head:
            raise GitCommitError(
                "Git commit did not advance HEAD",
                details=self._failure(
                    GitDeliveryFailureCode.COMMIT_MISSING,
                    baseline_head=change_set.baseline_head,
                    current_head=commit_sha,
                    expected=change_set.changed_paths,
                    actual=current_after_stage,
                    staged=staged_after_stage,
                    ownership=GitOwnershipStatus.VERIFIED,
                    summary="Git reported success without creating a Task commit.",
                ),
            )
        parent = self._required(("git", "rev-parse", "HEAD^"), cwd=root).strip()
        if parent != change_set.baseline_head:
            raise UnexpectedGitHead(
                "Task commit is not based on the recorded HEAD",
                details=self._failure(
                    GitDeliveryFailureCode.UNEXPECTED_HEAD_CHANGE,
                    baseline_head=change_set.baseline_head,
                    current_head=commit_sha,
                    expected=change_set.changed_paths,
                    actual=current_after_stage,
                    staged=staged_after_stage,
                    task_commit=commit_sha,
                    ownership=GitOwnershipStatus.UNCERTAIN,
                    summary="Task commit is not based on the recorded baseline HEAD.",
                ),
            )
        if self._required(
            ("git", "status", "--short", "--untracked-files=all"), cwd=root
        ).strip():
            changed_after, _, staged_after = self._status_paths(root)
            raise GitCommitError(
                "repository is not clean after Task commit",
                details=self._failure(
                    GitDeliveryFailureCode.COMMIT_CREATION_FAILED,
                    baseline_head=change_set.baseline_head,
                    current_head=commit_sha,
                    expected=change_set.changed_paths,
                    actual=changed_after,
                    staged=staged_after,
                    task_commit=commit_sha,
                    ownership=GitOwnershipStatus.UNCERTAIN,
                    summary="Repository is not clean after the Task commit.",
                ),
            )
        return GitCommitResult(
            task_id=task.id,
            repository_root=str(root),
            baseline_head=change_set.baseline_head,
            commit_sha=commit_sha,
            commit_message=message,
            changed_paths=change_set.changed_paths,
            staged_paths=staged,
            committed_at=self._clock(),
        )

    def _assert_root(self, root: Path) -> None:
        current = Path(
            self._required(("git", "rev-parse", "--show-toplevel"), cwd=root).strip()
        ).resolve()
        if current != root.resolve():
            raise GitOwnershipError("repository root changed during Task delivery")

    def _assert_head(self, root: Path, expected: str) -> None:
        current = self._required(("git", "rev-parse", "HEAD"), cwd=root).strip()
        if current != expected:
            raise UnexpectedGitHead(
                "repository HEAD changed during Task execution",
                details=self._failure(
                    GitDeliveryFailureCode.UNEXPECTED_HEAD_CHANGE,
                    baseline_head=expected,
                    current_head=current,
                    ownership=GitOwnershipStatus.MISMATCH,
                    summary="Repository HEAD changed outside the Task delivery boundary.",
                ),
            )

    def _status_paths(
        self, root: Path
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        raw = self._required(
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            cwd=root,
        )
        records = raw.split("\0")
        changed: list[str] = []
        untracked: list[str] = []
        staged: list[str] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if record == "":
                continue
            if len(record) < 4 or record[2] != " ":
                raise GitOwnershipError("Git returned an invalid porcelain status")
            code, path = record[:2], record[3:]
            paths = [path]
            if code[0] in {"R", "C"}:
                if index >= len(records) or records[index] == "":
                    raise GitOwnershipError("Git rename status is incomplete")
                paths.append(records[index])
                index += 1
            for item in paths:
                if item not in changed:
                    changed.append(item)
                if code == "??" and item not in untracked:
                    untracked.append(item)
                if code[0] not in {" ", "?"} and item not in staged:
                    staged.append(item)
        return tuple(sorted(changed)), tuple(sorted(untracked)), tuple(sorted(staged))

    @staticmethod
    def _normalize_owned_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw in paths:
            path = Path(raw)
            if path.is_absolute() or raw in {"", ".", ".."} or ".." in path.parts:
                raise GitOwnershipError("Worker reported an unsafe changed path")
            value = path.as_posix()
            if value not in normalized:
                normalized.append(value)
        return tuple(sorted(normalized))

    @staticmethod
    def _validate_report(report: ExecutionReport) -> None:
        if report.status != "completed" or report.human_action_required:
            raise WorkerVerificationError("Worker did not provide completed delivery evidence")
        if any(
            not isinstance(entries, tuple) or not all(isinstance(entry, str) for entry in entries)
            for entries in (report.tests, report.static_checks)
        ):
            raise WorkerVerificationError("Worker verification evidence is malformed")
        checks = report.verification_checks
        if checks is None:
            checks = legacy_checks(report.tests, report.static_checks)
        elif not evidence_matches_text(checks, report.tests, report.static_checks):
            raise WorkerVerificationError("Worker verification evidence is inconsistent")
        blocking = blocking_checks(checks)
        if blocking:
            raise WorkerVerificationError(
                "Worker verification evidence is not passing",
                blocking[0],
                unmet_checks=blocking,
            )

    @staticmethod
    def _nul_paths(raw: str) -> tuple[str, ...]:
        return tuple(sorted(path for path in raw.split("\0") if path))

    def _required(self, arguments: Sequence[str], cwd: Path | None = None) -> str:
        result = self._run(arguments, cwd or self._workspace)
        if result.returncode != 0:
            raise GitOwnershipError("required Git inspection failed")
        return result.stdout

    def _run(self, arguments: Sequence[str], cwd: Path) -> GitCommandResult:
        return self._runner(tuple(arguments), cwd)

    def _head(self, root: Path) -> str | None:
        result = self._run(("git", "rev-parse", "HEAD"), root)
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None

    @staticmethod
    def _failure(
        code: GitDeliveryFailureCode,
        *,
        baseline_head: str | None = None,
        current_head: str | None = None,
        expected: tuple[str, ...] = (),
        actual: tuple[str, ...] = (),
        staged: tuple[str, ...] = (),
        task_commit: str | None = None,
        ownership: GitOwnershipStatus,
        summary: str,
    ) -> GitDeliveryFailureDetails:
        return GitDeliveryFailureDetails(
            code,
            baseline_head,
            current_head,
            expected,
            actual,
            staged,
            task_commit,
            ownership,
            False,
            summary,
        )


def commit_message(task: Task) -> str:
    """Create a bounded one-line subject without consulting a model."""

    title = re.sub(r"\s+", " ", task.title.replace("\x00", " ")).strip()
    title = re.sub(r"[^\w .:+/#-]", "", title, flags=re.UNICODE).strip(" .")
    if title:
        return f"feat(task): {title}"[:72].rstrip()
    task_id = re.sub(r"[^A-Za-z0-9._-]", "-", task.id).strip("-") or "task"
    return f"chore(task): complete {task_id}"[:72]


__all__ = [
    "GitCommandResult",
    "GitCommandRunner",
    "GitDeliveryService",
    "commit_message",
    "run_git_command",
]
