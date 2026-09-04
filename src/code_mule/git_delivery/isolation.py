"""Repository-local isolation for Code Mule runtime state."""

from collections.abc import Sequence
from pathlib import Path

from .service import GitCommandRunner, run_git_command


class GitWorkspaceIsolationError(RuntimeError):
    """Raised when Code Mule state cannot be safely isolated from Git."""


def register_state_exclusion(
    workspace: Path,
    state_file: Path,
    *,
    runner: GitCommandRunner = run_git_command,
) -> tuple[str, ...]:
    """Exclude only Code Mule-managed state paths in repository-local Git config."""

    workspace = workspace.resolve()
    state_file = state_file.resolve()
    if not state_file.is_relative_to(workspace):
        return ()

    root = Path(
        _required(runner, ("git", "rev-parse", "--show-toplevel"), workspace)
    ).resolve()
    if not state_file.is_relative_to(root):
        raise GitWorkspaceIsolationError(
            "Code Mule state is inside the workspace but outside its Git repository"
        )

    relative_state = state_file.relative_to(root)
    relative_parent = relative_state.parent
    if relative_parent == Path(".code-mule"):
        patterns = ("/.code-mule/",)
    else:
        relative_lock = relative_parent / "execution.lock"
        patterns = (
            f"/{relative_state.as_posix()}",
            f"/{relative_lock.as_posix()}",
        )

    exclude_value = _required(
        runner, ("git", "rev-parse", "--git-path", "info/exclude"), root
    ).strip()
    if exclude_value == "":
        raise GitWorkspaceIsolationError("Git did not provide an info/exclude path")
    exclude_path = Path(exclude_value)
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    _append_missing(exclude_path, patterns)
    return patterns


def _required(
    runner: GitCommandRunner,
    arguments: Sequence[str],
    cwd: Path,
) -> str:
    result = runner(arguments, cwd)
    if result.returncode != 0:
        raise GitWorkspaceIsolationError("Git workspace could not be inspected safely")
    return result.stdout.strip()


def _append_missing(exclude_path: Path, patterns: tuple[str, ...]) -> None:
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        current = (
            exclude_path.read_text(encoding="utf-8")
            if exclude_path.exists()
            else ""
        )
        existing = frozenset(current.splitlines())
        missing = tuple(pattern for pattern in patterns if pattern not in existing)
        if not missing:
            return
        separator = "" if current == "" or current.endswith("\n") else "\n"
        with exclude_path.open("a", encoding="utf-8") as exclude_file:
            exclude_file.write(separator + "\n".join(missing) + "\n")
    except OSError as error:
        raise GitWorkspaceIsolationError(
            "repository-local Git exclude could not be updated"
        ) from error


__all__ = ["GitWorkspaceIsolationError", "register_state_exclusion"]
