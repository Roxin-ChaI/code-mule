"""Bounded metadata and diagnostics for persistent Code Mule installations."""

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Mapping

from code_mule import __version__
from code_mule.state import CURRENT_SCHEMA_VERSION, MIN_SUPPORTED_SCHEMA_VERSION


_MARKER = "CODE_MULE_INSTALL"
_MAX_MARKER_BYTES = 8192
_REVISION = re.compile(r"[0-9a-f]{7,64}")
_ALLOWED_KEYS = {
    "version",
    "prefix",
    "source",
    "launcher",
    "model_dependency",
    "installed_at",
    "source_revision",
    "schema_min",
    "schema_max",
    "status",
}


@dataclass(frozen=True)
class InstallMetadata:
    package_version: str
    source_revision: str | None
    schema_min: int | None
    schema_max: int | None
    installed_at: str
    prefix: Path
    source: Path | None
    launcher: Path | None


@dataclass(frozen=True)
class InstallationDiagnostics:
    executable: Path
    resolved_executable: Path
    package_root: Path
    cli_version: str
    build_revision: str | None
    schema_min: int
    schema_max: int
    global_install_status: str
    install_metadata: InstallMetadata | None
    update_command: str | None


def _bounded(value: str, limit: int = 256) -> str:
    return value.strip()[:limit]


def read_install_metadata(path: Path) -> InstallMetadata | None:
    """Read only known, bounded installer fields from one ownership marker."""

    try:
        if path.stat().st_size > _MAX_MARKER_BYTES:
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0] != "Code Mule persistent installation" or len(lines) > 32:
        return None
    values: dict[str, str] = {}
    for line in lines[1:]:
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        if key in _ALLOWED_KEYS:
            values[key] = _bounded(value)
    try:
        prefix = Path(values["prefix"]).expanduser().resolve()
        installed_at = values["installed_at"]
        package_version = values["version"]
    except (KeyError, TypeError, ValueError, OSError):
        return None
    try:
        minimum = int(values["schema_min"])
        maximum = int(values["schema_max"])
    except (KeyError, ValueError):
        minimum = maximum = None
    revision = values.get("source_revision")
    if revision is not None and _REVISION.fullmatch(revision) is None:
        revision = None
    source_value = values.get("source")
    launcher_value = values.get("launcher")
    return InstallMetadata(
        package_version=_bounded(package_version, 64),
        source_revision=revision,
        schema_min=minimum,
        schema_max=maximum,
        installed_at=_bounded(installed_at, 64),
        prefix=prefix,
        source=None if not source_value else Path(source_value).expanduser().resolve(),
        launcher=None if not launcher_value else Path(launcher_value).expanduser().resolve(),
    )


def _source_root(package_root: Path) -> Path | None:
    candidate = package_root.parent.parent if package_root.parent.name == "src" else None
    if candidate is None:
        return None
    if not (candidate / ".git").exists() or not (candidate / "scripts" / "install.sh").is_file():
        return None
    return candidate.resolve()


def _git_revision(root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and _REVISION.fullmatch(revision) else None


def _marker_candidates(
    executable: Path, resolved: Path, environment: Mapping[str, str]
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    home = environment.get("HOME")
    if home:
        candidates.append(Path(home).expanduser() / ".local/share/code-mule" / _MARKER)
    for value in (executable, resolved):
        parents = value.parents
        if len(parents) >= 3 and parents[0].name == "bin" and parents[1].name == "venv":
            candidates.append(parents[2] / _MARKER)
    return tuple(dict.fromkeys(candidates))


def diagnose_installation(
    *,
    environment: Mapping[str, str] | None = None,
    executable: Path | None = None,
    package_root: Path | None = None,
) -> InstallationDiagnostics:
    """Describe this executable without treating arbitrary workspaces as source trees."""

    runtime = dict(environment or {})
    invoked = executable
    if invoked is None:
        located = shutil.which("code-mule", path=runtime.get("PATH"))
        invoked = Path(located) if located else Path(sys.argv[0])
    invoked = invoked.expanduser().absolute()
    resolved = invoked.resolve()
    root = (package_root or Path(__file__).resolve().parent).resolve()
    source = _source_root(root)
    source_revision = _git_revision(source)
    metadata = next(
        (
            value
            for marker in _marker_candidates(invoked, resolved, runtime)
            if (value := read_install_metadata(marker)) is not None
            and (
                value.launcher is None
                or value.launcher == invoked.resolve()
                or value.launcher == invoked
            )
        ),
        None,
    )
    is_global = metadata is not None
    if not is_global:
        status = "NOT GLOBAL"
    elif (
        source is not None
        and metadata.source is not None
        and metadata.source == source
        and (
            metadata.source_revision is None
            or metadata.schema_min is None
            or metadata.schema_max is None
            or (
                source_revision is not None
                and source_revision != metadata.source_revision
            )
        )
    ):
        status = "STALE GLOBAL INSTALL"
    else:
        status = "MANAGED"
    build_revision = (
        metadata.source_revision if metadata is not None else source_revision
    )
    update_root = (
        metadata.source
        if metadata is not None and metadata.source is not None
        else source
    )
    update_command = None
    if update_root is not None and (update_root / "scripts/install.sh").is_file():
        update_command = f"bash {update_root / 'scripts/install.sh'} --reinstall"
    return InstallationDiagnostics(
        executable=invoked,
        resolved_executable=resolved,
        package_root=root,
        cli_version=__version__,
        build_revision=build_revision,
        schema_min=MIN_SUPPORTED_SCHEMA_VERSION,
        schema_max=CURRENT_SCHEMA_VERSION,
        global_install_status=status,
        install_metadata=metadata,
        update_command=update_command,
    )


def render_version(diagnostics: InstallationDiagnostics, *, verbose: bool) -> tuple[str, ...]:
    if not verbose:
        return (f"Code Mule {diagnostics.cli_version}",)
    revision = diagnostics.build_revision or "unknown"
    lines = (
        f"Code Mule {diagnostics.cli_version}",
        f"Executable      {diagnostics.executable}",
        f"Package root    {diagnostics.package_root}",
        f"Build revision  {revision}",
        f"Schema support  {diagnostics.schema_min}..{diagnostics.schema_max}",
        f"Global install  {diagnostics.global_install_status}",
    )
    metadata = diagnostics.install_metadata
    if metadata is not None:
        lines += (f"Installed at    {metadata.installed_at}",)
    if diagnostics.global_install_status == "STALE GLOBAL INSTALL" and diagnostics.update_command:
        lines += ("Update required", f"Update command  {diagnostics.update_command}")
    return lines


__all__ = [
    "InstallMetadata",
    "InstallationDiagnostics",
    "diagnose_installation",
    "read_install_metadata",
    "render_version",
]
