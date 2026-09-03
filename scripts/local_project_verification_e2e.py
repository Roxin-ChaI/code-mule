"""Disposable project-level verification E2E without model API calls."""

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_ROOT = Path(__file__).resolve().parents[1]
for path in (_ROOT, _ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from local_git_delivery_e2e import initial_state, run_scenario  # noqa: E402

from code_mule.domain import TaskStatus  # noqa: E402
from code_mule.git_delivery import GitCommitResult  # noqa: E402
from code_mule.project_verification import (  # noqa: E402
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCommand,
    ProjectVerificationSpec,
)
from code_mule.project_verification.service import (  # noqa: E402
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor import FinalReviewResult  # noqa: E402


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class IdFactory:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"


class ApprovingFinalSupervisor:
    def __init__(self) -> None:
        self.calls = 0

    def final_review(self, request):
        self.calls += 1
        return FinalReviewResult(
            FinalReviewDecision.APPROVE,
            "Fake final Supervisor approved the persisted evidence.",
            (),
        )


def gated_scenario(*, dirty: bool) -> dict[str, object]:
    with TemporaryDirectory(prefix="code-mule-final-e2e-") as directory:
        root = Path(directory)
        repository = root / "repository"
        repository.mkdir()
        git(repository, "init", "-q")
        git(repository, "config", "user.name", "Code Mule E2E")
        git(repository, "config", "user.email", "code-mule@example.invalid")
        (repository / "README.md").write_text("Final verification E2E\n")
        git(repository, "add", "--", "README.md")
        git(repository, "commit", "-q", "-m", "initial")
        baseline = git(repository, "rev-parse", "HEAD")
        (repository / "multiply.py").write_text(
            "def multiply(left, right):\n    return left * right\n"
        )
        git(repository, "add", "--", "multiply.py")
        git(repository, "commit", "-q", "-m", "feat(task): Add multiply support")
        delivered = git(repository, "rev-parse", "HEAD")

        state = initial_state(repository, rework=True)
        task = replace(state.tasks[0], status=TaskStatus.COMPLETED)
        command = ProjectVerificationCommand(
            "project tests",
            ProjectVerificationCategory.TEST,
            (
                sys.executable,
                "-c",
                "raise SystemExit(1)" if not dirty else "raise SystemExit(0)",
            ),
            timeout_seconds=30,
        )
        state = replace(
            state,
            tasks=(task,),
            git_commit_results=(
                GitCommitResult(
                    task.id,
                    str(repository),
                    baseline,
                    delivered,
                    "feat(task): Add multiply support",
                    ("multiply.py",),
                    ("multiply.py",),
                    datetime.now(UTC),
                ),
            ),
            project_verification_spec=ProjectVerificationSpec(
                state.project.id, (command,)
            ),
        )
        store = JsonProjectStateStore(root / "project-state.json")
        store.save(state)
        if dirty:
            (repository / "external.txt").write_text("unowned\n")
        supervisor = ApprovingFinalSupervisor()
        final = ProjectFinalizationService(
            store=store,
            verification=ProjectVerificationService(
                clock=lambda: datetime.now(UTC),
                result_id_factory=IdFactory("verification"),
            ),
            supervisor=supervisor,
            clock=lambda: datetime.now(UTC),
            event_id_factory=IdFactory("event"),
        ).finalize(store.load())
        result = final.project_verification_results[-1]
        return {
            "project_status": final.project.status.value,
            "failed_checks": [
                check.name
                for check in result.checks
                if check.required and check.status.value != "pass"
            ],
            "final_supervisor_calls": supervisor.calls,
            "result_persisted": len(final.project_verification_results) == 1,
        }


def main() -> int:
    payload = {
        "success": run_scenario(real_worker=False, rework=False),
        "test_failure": gated_scenario(dirty=False),
        "dirty_git": gated_scenario(dirty=True),
    }
    print(json.dumps(payload, sort_keys=True))
    valid = (
        payload["success"]["project_status"] == "done"
        and payload["success"]["commit_count"] == 2
        and payload["test_failure"]["project_status"] == "human_required"
        and payload["test_failure"]["final_supervisor_calls"] == 0
        and payload["dirty_git"]["project_status"] == "human_required"
        and payload["dirty_git"]["final_supervisor_calls"] == 0
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
