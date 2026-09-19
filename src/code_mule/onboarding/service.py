"""Deterministic local-only environment checks for the Boss workflow."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Mapping

from code_mule import __version__
from code_mule.installation import diagnose_installation
from code_mule.state.serialization import StateSchemaCompatibilityError
from code_mule.state.models import ProjectState
from code_mule.state.store import JsonProjectStateStore

from .contracts import (
    DoctorCheck,
    DoctorReport,
    StartDecision,
    StartPreflight,
    WorkspaceProbe,
)


@dataclass(frozen=True)
class LocalCommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class CodexTransportProbe:
    """Read-only Codex CLI facts.  Never starts a model turn."""

    binary: str | None
    version: str | None
    app_server_available: bool


def probe_codex_transport(
    environment: Mapping[str, str] | None = None,
) -> CodexTransportProbe:
    """Probe only the CLI binary, its version, and app-server availability."""

    codex = _which("codex", environment)
    if codex is None:
        return CodexTransportProbe(None, None, False)
    version: str | None = None
    version_result = run_local_command(
        (str(codex), "--version"), Path.cwd(), environment=environment
    )
    if version_result.returncode == 0:
        version = safe_version(version_result.stdout)
    server_result = run_local_command(
        (str(codex), "app-server", "--help"), Path.cwd(), environment=environment
    )
    return CodexTransportProbe(str(codex), version, server_result.returncode == 0)


_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]+)?")
_STATUS_ENTRY_LIMIT = 8


def _runtime_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    runtime = dict(os.environ)
    if environment is not None:
        runtime.update(environment)
    return runtime


def run_local_command(
    arguments: tuple[str, ...],
    cwd: Path,
    *,
    environment: Mapping[str, str] | None = None,
    timeout: int = 10,
) -> LocalCommandResult:
    """Run one bounded local command with a deterministic failure envelope."""

    try:
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=_runtime_environment(environment),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except OSError:
        return LocalCommandResult(127, "", "local command could not be executed")
    except subprocess.TimeoutExpired:
        return LocalCommandResult(124, "", "local command timed out")
    return LocalCommandResult(completed.returncode, completed.stdout, completed.stderr)


def safe_version(output: str) -> str | None:
    """Extract one bounded semantic-ish version from command output."""

    match = _VERSION_PATTERN.search(output or "")
    return None if match is None else match.group(0)[:64]


def _which(name: str, environment: Mapping[str, str]) -> Path | None:
    candidate = shutil.which(name, path=environment.get("PATH"))
    return Path(candidate) if candidate else None


def _cli_location(environment: Mapping[str, str]) -> str:
    located = _which("code-mule", environment)
    if located is not None:
        return str(located)
    invoked = Path(sys.argv[0]).resolve() if sys.argv else None
    if invoked is not None and invoked.exists() and invoked.is_file():
        return str(invoked)
    return "not on PATH"


def workspace_probe(
    workspace: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> WorkspaceProbe:
    """Probe repository, HEAD, and working-tree facts without any mutation."""

    resolved = workspace.resolve()
    runtime = dict(os.environ) if environment is None else dict(environment)
    git = _which("git", runtime)
    if git is None:
        return WorkspaceProbe(
            git_available=False,
            issue="Git is not installed or is not available on PATH.",
        )
    root_result = run_local_command(
        ("git", "rev-parse", "--show-toplevel"),
        resolved,
        environment=runtime,
    )
    if root_result.returncode != 0:
        return WorkspaceProbe(
            git_available=True,
            issue="Not inside a Git repository.",
        )
    root = Path(root_result.stdout.strip()).resolve()
    head_result = run_local_command(
        ("git", "rev-parse", "--verify", "HEAD"),
        root,
        environment=runtime,
    )
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    status_result = run_local_command(
        ("git", "status", "--short", "--untracked-files=all"),
        root,
        environment=runtime,
    )
    entries = tuple(line for line in status_result.stdout.splitlines() if line)
    clean = head is not None and not entries
    if head is None:
        issue = "Git repository has no initial commit yet."
    elif entries:
        issue = "Git working tree is not clean."
    else:
        issue = None
    return WorkspaceProbe(
        git_available=True,
        repository_root=root,
        head=head,
        clean=clean,
        status_entries=entries[:_STATUS_ENTRY_LIMIT],
        issue=issue,
    )


def workspace_slug(name: str) -> str:
    """Deterministic safe project id from a directory name."""

    lowered = (name or "").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return (slug or "project")[:80]


class DoctorService:
    """Local-only doctor over code, tools, configuration, and workspace."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._environment = (
            dict(os.environ) if environment is None else dict(environment)
        )

    def diagnose(self, workspace: Path, state_file: Path) -> DoctorReport:
        workspace = workspace.resolve()
        state_file = state_file.expanduser().resolve()
        return DoctorReport(
            (
                self._code_mule_check(),
                self._python_check(),
                self._git_check(),
                self._codex_check(),
                self._deepseek_check(),
                self._workspace_check(workspace, state_file),
            )
        )

    def _code_mule_check(self) -> DoctorCheck:
        location = _cli_location(self._environment)
        diagnostics = diagnose_installation(
            environment=self._environment,
            executable=None if location == "not on PATH" else Path(location),
        )
        details = (
            f"version  {__version__}",
            f"cli      {location}",
            f"revision {diagnostics.build_revision or 'unknown'}",
            f"schema   {diagnostics.schema_min}..{diagnostics.schema_max}",
            f"install  {diagnostics.global_install_status}",
        )
        if diagnostics.global_install_status == "STALE GLOBAL INSTALL":
            advice = (
                "Update required: the persistent CLI was built from an older source revision.",
            )
            if diagnostics.update_command is not None:
                advice += (diagnostics.update_command,)
            return DoctorCheck(
                "Code Mule", "STALE GLOBAL INSTALL", False,
                details=details, advice=advice,
            )
        return DoctorCheck(
            "Code Mule",
            "PASS",
            True,
            details=details,
        )

    def _python_check(self) -> DoctorCheck:
        import platform

        return DoctorCheck(
            "Python",
            "PASS",
            True,
            details=(
                f"runtime  {platform.python_version()}",
                "requirement 3.12",
            ),
        )

    def _git_check(self) -> DoctorCheck:
        git = _which("git", self._environment)
        if git is None:
            return DoctorCheck(
                "Git",
                "MISSING",
                False,
                advice=(
                    "Install Git and make sure it is available on PATH.",
                ),
            )
        result = run_local_command(
            ("git", "--version"),
            Path.cwd(),
            environment=self._environment,
        )
        if result.returncode != 0:
            return DoctorCheck(
                "Git",
                "FAIL",
                False,
                details=(f"binary   {git}",),
                advice=("Git is present but did not start; check the installation.",),
            )
        version = safe_version(result.stdout)
        details = (f"binary   {git}",)
        if version is not None:
            details += (f"version  {version}",)
        return DoctorCheck("Git", "PASS", True, details=details)

    def _codex_check(self) -> DoctorCheck:
        codex = _which("codex", self._environment)
        if codex is None:
            return DoctorCheck(
                "Codex",
                "MISSING",
                False,
                advice=(
                    "Install the Codex CLI and authenticate locally before starting work.",
                ),
            )
        version_result = run_local_command(
            (str(codex), "--version"),
            Path.cwd(),
            environment=self._environment,
        )
        if version_result.returncode != 0:
            return DoctorCheck(
                "Codex",
                "FAIL",
                False,
                details=(f"binary   {codex}",),
                advice=(
                    "Codex CLI is present but did not start; check the installation.",
                ),
            )
        server_result = run_local_command(
            (str(codex), "app-server", "--help"),
            Path.cwd(),
            environment=self._environment,
        )
        version = safe_version(version_result.stdout)
        details = (f"binary   {codex}",)
        if version is not None:
            details += (f"version  {version}",)
        if server_result.returncode != 0:
            return DoctorCheck(
                "Codex",
                "FAIL",
                False,
                details=details,
                advice=(
                    "This Codex CLI does not expose `codex app-server`, which Code Mule requires.",
                    "No minimum-version claim is made; update to a CLI that provides it.",
                ),
            )
        return DoctorCheck(
            "Codex",
            "PASS",
            True,
            details=details + ("app server  codex app-server (available)",),
        )

    def _deepseek_check(self) -> DoctorCheck:
        if self._environment.get("DEEPSEEK_API_KEY"):
            return DoctorCheck("DeepSeek", "CONFIGURED", True)
        return DoctorCheck(
            "DeepSeek",
            "NOT CONFIGURED",
            False,
            advice=(
                "Set DEEPSEEK_API_KEY in your shell before starting model work.",
                "Do not commit or paste the key.",
            ),
        )

    def _workspace_check(self, workspace: Path, state_file: Path) -> DoctorCheck:
        probe = workspace_probe(workspace, environment=self._environment)
        details: list[str] = []
        if probe.repository_root is not None:
            details.append(f"repository {probe.repository_root}")
        if probe.head is not None:
            details.append(f"head      {probe.head[:12]}")
        if probe.clean is not None:
            details.append(f"worktree  {'clean' if probe.clean else 'dirty'}")
        state_status: str | None = None
        state_invalid = False
        state_advice = (
            "A Code Mule state file exists but cannot be read. "
            "Do not run start again; inspect or repair the state file yourself."
        )
        if state_file.exists():
            store = JsonProjectStateStore(state_file)
            try:
                state: ProjectState | None = store.load()
                state_status = None if state is None else state.project.status.value
                if state is not None:
                    details.append(f"project   {state.project.name}")
                    details.append(f"status    {state_status}")
            except StateSchemaCompatibilityError as error:
                state_invalid = True
                compatibility = error.compatibility
                details.append(f"state     {compatibility.code.value}")
                if compatibility.project_schema is not None:
                    details.append(f"project schema {compatibility.project_schema}")
                details.append(
                    f"cli schema {compatibility.supported_min}..{compatibility.supported_max}"
                )
                if compatibility.code.value == "CLI_TOO_OLD":
                    state_advice = (
                        "Update required: use the installed Code Mule source installer, "
                        "then rerun doctor. The state file was not modified."
                    )
            except Exception:
                state_invalid = True
                details.append("state     present but unreadable")
        else:
            details.append("state     not initialized")

        if not probe.git_available or probe.issue is not None:
            advice = tuple(
                filter(
                    None,
                    (
                        probe.issue,
                        (
                            "Initialize Git and create an initial commit yourself; "
                            "Code Mule never runs git init automatically."
                            if probe.git_available and probe.head is None
                            else None
                        ),
                        (
                            "Commit or move the changes yourself; "
                            "Code Mule never stashes, resets, or cleans."
                            if probe.clean is False
                            else None
                        ),
                    ),
                )
            )
            return DoctorCheck(
                "Workspace",
                "PROBLEM",
                False,
                details=tuple(details),
                advice=advice,
            )
        if state_invalid:
            return DoctorCheck(
                "Workspace",
                "PROBLEM",
                False,
                details=tuple(details),
                advice=(state_advice,),
            )
        return DoctorCheck(
            "Workspace",
            "READY",
            True,
            details=tuple(details),
        )


class StartPreflightService:
    """Read-only Git/state preflight used by `code-mule start`."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._environment = (
            dict(os.environ) if environment is None else dict(environment)
        )

    def preflight(self, workspace: Path, state_file: Path) -> StartPreflight:
        workspace = workspace.expanduser().resolve()
        state_file = state_file.expanduser().resolve()
        state_exists = state_file.exists()
        probe = workspace_probe(workspace, environment=self._environment)

        if state_exists:
            return StartPreflight(
                decision=StartDecision.EXISTING_PROJECT,
                workspace=workspace,
                state_file=state_file,
                state_exists=True,
                probe=probe,
                heading="EXISTING PROJECT",
                reason="Existing Code Mule project detected; no re-initialization was performed.",
            )
        if not probe.git_available:
            return StartPreflight(
                decision=StartDecision.NOT_GIT_REPO,
                workspace=workspace,
                state_file=state_file,
                state_exists=False,
                probe=probe,
                heading="GIT REPOSITORY REQUIRED",
                reason="Git is not installed or is not available on PATH.",
                next_commands=("Install Git, then rerun start.",),
            )
        if probe.repository_root is None:
            return StartPreflight(
                decision=StartDecision.NOT_GIT_REPO,
                workspace=workspace,
                state_file=state_file,
                state_exists=False,
                probe=probe,
                heading="GIT REPOSITORY REQUIRED",
                reason=probe.issue or "Not inside a Git repository.",
                next_commands=(
                    "git init",
                    "git add README.md",
                    'git commit -m "chore: initial commit"',
                    'code-mule start --objective "..."',
                ),
            )
        if probe.head is None:
            return StartPreflight(
                decision=StartDecision.NO_GIT_HEAD,
                workspace=workspace,
                state_file=state_file,
                state_exists=False,
                probe=probe,
                heading="INITIAL GIT COMMIT REQUIRED",
                reason="This Git repository has no commit yet; Code Mule does not create Boss commits.",
                next_commands=(
                    "git add README.md",
                    'git commit -m "chore: initial commit"',
                    'code-mule start --objective "..."',
                ),
            )
        if probe.status_entries:
            return StartPreflight(
                decision=StartDecision.DIRTY_WORKSPACE,
                workspace=workspace,
                state_file=state_file,
                state_exists=False,
                probe=probe,
                heading="WORKSPACE BLOCKED",
                reason="The Git working tree is not clean; Code Mule never stashes, resets, or cleans.",
                next_commands=(
                    "git status",
                    'code-mule start --objective "..."',
                ),
            )
        return StartPreflight(
            decision=StartDecision.READY_TO_INIT,
            workspace=workspace,
            state_file=state_file,
            state_exists=False,
            probe=probe,
            heading="START READY",
        )


__all__ = [
    "CodexTransportProbe",
    "DoctorService",
    "LocalCommandResult",
    "StartPreflightService",
    "probe_codex_transport",
    "run_local_command",
    "safe_version",
    "workspace_probe",
    "workspace_slug",
]
