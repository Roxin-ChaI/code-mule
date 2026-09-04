"""Deterministic, path-scoped local Git delivery service."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import subprocess
from typing import Protocol

from code_mule.domain.models import ExecutionReport, Task

from .contracts import (
    DirtyGitBaseline,
    EmptyGitChangeSet,
    GitBaseline,
    GitChangeSet,
    GitCommitError,
    GitCommitResult,
    GitOwnershipError,
    GitStagingError,
    UnexpectedGitHead,
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
            raise GitOwnershipError("workspace is outside the discovered repository")
        status = self._required(
            ("git", "status", "--short", "--untracked-files=all"), cwd=root
        )
        entries = tuple(line for line in status.splitlines() if line)
        if entries:
            error = DirtyGitBaseline("repository must be clean before Task dispatch")
            error.status_entries = entries
            raise error
        head = self._required(("git", "rev-parse", "HEAD"), cwd=root).strip()
        return GitBaseline(task_id, str(root), head, entries)

    def prepare_change_set(
        self,
        baseline: GitBaseline,
        report: ExecutionReport,
        owned_paths: tuple[str, ...],
    ) -> GitChangeSet:
        root = Path(baseline.repository_root)
        self._validate_report(report)
        self._assert_root(root)
        self._assert_head(root, baseline.baseline_head)
        self._required(("git", "diff", "--check"), cwd=root)
        changed, untracked, staged = self._status_paths(root)
        if not changed:
            raise EmptyGitChangeSet(
                "accepted implementation Task produced no repository changes"
            )
        expected = self._normalize_owned_paths(owned_paths)
        if set(changed) != set(expected):
            raise GitOwnershipError(
                "repository changes do not exactly match Worker-owned paths"
            )
        if staged:
            raise GitOwnershipError(
                "Worker must not stage paths; staging belongs to the Orchestrator"
            )
        return GitChangeSet(
            task_id=baseline.task_id,
            repository_root=str(root),
            baseline_head=baseline.baseline_head,
            changed_paths=changed,
            untracked_paths=untracked,
            staged_paths=(),
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
                "Worker must not stage paths; staging belongs to the Orchestrator"
            )
        return changed

    def commit(self, change_set: GitChangeSet, task: Task) -> GitCommitResult:
        root = Path(change_set.repository_root)
        self._assert_root(root)
        self._assert_head(root, change_set.baseline_head)
        current, _, staged_before = self._status_paths(root)
        if current != change_set.changed_paths or staged_before:
            raise GitOwnershipError("repository changed after ownership validation")

        stage = self._run(("git", "add", "--", *change_set.changed_paths), root)
        if stage.returncode != 0:
            raise GitStagingError("precise Git staging failed")
        staged = self._nul_paths(
            self._required(
                ("git", "diff", "--cached", "--name-only", "-z"), cwd=root
            )
        )
        if staged != change_set.changed_paths:
            raise GitStagingError("staged paths do not exactly match the Task change set")
        cached_check = self._run(("git", "diff", "--cached", "--check"), root)
        if cached_check.returncode != 0:
            raise GitStagingError("staged Task change fails git diff --check")
        current_after_stage, _, staged_after_stage = self._status_paths(root)
        if (
            current_after_stage != change_set.changed_paths
            or staged_after_stage != change_set.changed_paths
        ):
            raise GitOwnershipError("repository changed during precise staging")
        unstaged_check = self._run(("git", "diff", "--quiet", "--"), root)
        if unstaged_check.returncode != 0:
            raise GitOwnershipError("repository changed after Task paths were staged")
        self._assert_head(root, change_set.baseline_head)

        message = commit_message(task)
        committed = self._run(("git", "commit", "-m", message), root)
        if committed.returncode != 0:
            raise GitCommitError("Git commit failed")
        commit_sha = self._required(("git", "rev-parse", "HEAD"), cwd=root).strip()
        if commit_sha == change_set.baseline_head:
            raise GitCommitError("Git commit did not advance HEAD")
        parent = self._required(("git", "rev-parse", "HEAD^"), cwd=root).strip()
        if parent != change_set.baseline_head:
            raise UnexpectedGitHead("Task commit is not based on the recorded HEAD")
        if self._required(
            ("git", "status", "--short", "--untracked-files=all"), cwd=root
        ).strip():
            raise GitCommitError("repository is not clean after Task commit")
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
            raise UnexpectedGitHead("repository HEAD changed during Task execution")

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
            raise GitOwnershipError("Worker did not provide completed delivery evidence")
        checks = report.tests + report.static_checks
        if any(": pass" not in item.lower() for item in checks):
            raise GitOwnershipError("Worker verification evidence is not passing")

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
