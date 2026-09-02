"""Boss-only real DeepSeek + Codex E2E through the installed CLI."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main() -> int:
    print("REAL DEEPSEEK + CODEX CLI E2E — BOSS MANUAL ONLY")
    print("This makes real DeepSeek requests, uses local Codex, and may incur billing.")
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("ERROR: DEEPSEEK_API_KEY is required.", file=sys.stderr)
        return 2
    executable = Path(sys.executable).with_name("code-mule")
    with TemporaryDirectory(prefix="code-mule-cli-manual-") as temporary:
        root = Path(temporary)
        workspace = root / "repository"
        workspace.mkdir()
        _run(["git", "init", str(workspace)])
        state_file = root / "project-state.json"
        common = ["--state-file", str(state_file)]
        _run([
            str(executable), "init", "--project-id", "manual-cli",
            "--name", "Manual CLI calculator", "--workspace", str(workspace),
            *common,
        ])
        running = subprocess.Popen([
            str(executable), "run", "--objective",
            "Create a calculator with add and subtract plus tests.", *common,
        ])
        from code_mule.state.store import JsonProjectStateStore

        store = JsonProjectStateStore(state_file)
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            state = store.load()
            if state.project.current_task_id is not None:
                break
            if running.poll() is not None:
                raise RuntimeError("run stopped before a Task became active")
            time.sleep(0.2)
        else:
            running.terminate()
            raise RuntimeError("timed out waiting for an active Task")
        _run([str(executable), "change", "Add multiply support", *common])
        if running.wait(timeout=360) != 0:
            raise RuntimeError("run did not reach the CHANGE Safe Point")
        _run([str(executable), "status", *common])
        _run([str(executable), "change", "--apply", *common])
        _run([str(executable), "status", *common])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
