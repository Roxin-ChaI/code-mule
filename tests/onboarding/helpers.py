"""Shared deterministic fixtures for onboarding tests."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = ROOT / "scripts" / "install.sh"


def run_command(
    arguments,
    *,
    cwd=None,
    environment=None,
    check=False,
    timeout=120,
):
    base = dict(os.environ)
    if environment is not None:
        base.update(environment)
    completed = subprocess.run(
        tuple(str(argument) for argument in arguments),
        cwd=cwd,
        env=base,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {arguments}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def git_repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_command(("git", "init", "-q"), cwd=path, check=True)
    run_command(
        ("git", "config", "user.name", "Onboarding Test"),
        cwd=path,
        check=True,
    )
    run_command(
        ("git", "config", "user.email", "onboarding@example.invalid"),
        cwd=path,
        check=True,
    )
    if commit:
        (path / "README.md").write_text("# Project\n", encoding="utf-8")
        run_command(("git", "add", "README.md"), cwd=path, check=True)
        run_command(
            ("git", "commit", "-qm", "chore: initial commit"),
            cwd=path,
            check=True,
        )
    return path


def write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_codex(bin_dir: Path, *, version: str = "9.9.9") -> Path:
    return write_executable(
        bin_dir / "codex",
        f"""#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "codex-cli {version}"
  exit 0
fi
if [ "$1" = "app-server" ] && [ "$2" = "--help" ]; then
  exit 0
fi
echo "unexpected codex invocation" >&2
exit 1
""",
    )


def fake_code_mule(bin_dir: Path) -> Path:
    return write_executable(
        bin_dir / "code-mule",
        "#!/bin/sh\necho 'fake code-mule'\nexit 0\n",
    )


def git_system_paths() -> str:
    """Minimal deterministic PATH containing Git but never a user Codex CLI."""

    parts: list[str] = []
    git = shutil.which("git")
    if git is not None:
        parts.append(str(Path(git).parent))
    parts.extend(("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    return ":".join(parts)


@contextmanager
def chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
