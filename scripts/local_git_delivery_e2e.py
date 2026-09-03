"""Local Git delivery E2E with a fake Supervisor and optional real Codex Worker."""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for path in (_ROOT, _SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from code_mule.domain import (  # noqa: E402
    ExecutionReport,
    Milestone,
    Plan,
    PlanStatus,
    Project,
    ProjectStatus,
    Requirement,
    RequirementStatus,
    SupervisorDecisionType,
    Task,
    TaskStatus,
)
from code_mule.git_delivery import GitDeliveryService  # noqa: E402
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
from code_mule.runtime import (  # noqa: E402
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler  # noqa: E402
from code_mule.state.models import ProjectState  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor import ReviewResult  # noqa: E402
from code_mule.worker import (  # noqa: E402
    CodexWorkerConfig,
    CodexWorkerSession,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class IdFactory:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


class FakeSupervisor:
    def __init__(self, repository: Path, *, rework: bool) -> None:
        self.repository = repository
        self.rework = rework
        self.calls = 0
        self.commit_counts_at_review: list[int] = []

    def review(self, request):
        self.calls += 1
        count = int(git(self.repository, "rev-list", "--count", "HEAD"))
        self.commit_counts_at_review.append(count)
        if self.rework and self.calls == 1:
            return ReviewResult(
                SupervisorDecisionType.REWORK,
                "Exercise the no-commit REWORK boundary.",
                "Improve the implementation while preserving its public behavior.",
                (),
            )
        return ReviewResult(
            SupervisorDecisionType.CONTINUE,
            "Fake Supervisor accepted deterministic evidence.",
            None,
            (),
        )

    def final_review(self, request):
        from code_mule.supervisor import FinalReviewResult

        return FinalReviewResult(
            FinalReviewDecision.APPROVE,
            "Fake final Supervisor approved persisted verification evidence.",
            (),
        )


class FakeWorkerSession:
    def __init__(self, repository: Path) -> None:
        self.repository = repository

    @property
    def thread_id(self):
        return "fake-thread"

    def start(self):
        return None

    def execute(self, request, *, report_id, created_at):
        if request.task.id == "TASK-ADD":
            target = self.repository / "calculator.py"
            target.write_text(
                '"""Small calculator."""\n\ndef add(left, right):\n    return left + right\n',
                encoding="utf-8",
            )
            paths = ("calculator.py",)
        elif request.task.id == "TASK-TEST":
            target = self.repository / "test_calculator.py"
            target.write_text(
                "import unittest\n\nfrom calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )
            paths = ("test_calculator.py",)
        else:
            target = self.repository / "multiply.py"
            body = (
                '"""Reworked multiply implementation."""\n\n'
                if request.task.execution_attempts
                else '"""Multiply implementation."""\n\n'
            )
            target.write_text(
                body + "def multiply(left, right):\n    return left * right\n",
                encoding="utf-8",
            )
            paths = ("multiply.py",)
        return ExecutionReport(
            report_id,
            request.task.id,
            request.task.execution_attempts + 1,
            "completed",
            paths,
            ("unittest: pass",),
            ("compileall: pass",),
            "dirty",
            (),
            False,
            "Fake Worker completed the Task.",
            created_at,
        )

    def close(self):
        return None


def initial_state(repository: Path, *, rework: bool) -> ProjectState:
    requirement = Requirement(
        "REQ-1", "PROJECT", "Calculator", "Provide calculator behavior",
        RequirementStatus.ACTIVE, "high", ("behavior is tested",), "boss", NOW, NOW,
    )
    if rework:
        tasks = (
            Task(
                "TASK-MULTIPLY", "M1", "Add multiply support",
                "Create multiply.py with a multiply function.", TaskStatus.PENDING,
                (), ("multiply returns the product",), 0, NOW, NOW, ("REQ-1",),
            ),
        )
    else:
        tasks = (
            Task(
                "TASK-ADD", "M1", "Add calculator addition",
                "Only create calculator.py implementing add(left, right).",
                TaskStatus.PENDING, (), ("add returns the sum",), 0, NOW, NOW,
                ("REQ-1",),
            ),
            Task(
                "TASK-TEST", "M1", "Add calculator tests",
                "Create test_calculator.py using unittest and verify calculator.add.",
                TaskStatus.PENDING, ("TASK-ADD",), ("tests pass",), 0, NOW, NOW,
                ("REQ-1",),
            ),
        )
    verification_commands = (
        ()
        if rework
        else (
            ProjectVerificationCommand(
                "project unittest",
                ProjectVerificationCategory.TEST,
                (sys.executable, "-m", "unittest", "discover", "-s", ".", "-v"),
                timeout_seconds=60,
            ),
        )
    )
    return ProjectState(
        project=Project(
            "PROJECT", "Git Delivery E2E", ProjectStatus.RUNNING, "PLAN-1", None,
            NOW, NOW, str(repository),
        ),
        requirements=(requirement,),
        plans=(
            Plan(
                "PLAN-1", "PROJECT", 1, PlanStatus.ACTIVE, ("REQ-1",), ("M1",), NOW
            ),
        ),
        milestones=(Milestone("M1", "PLAN-1", "Delivery", "active", tuple(t.id for t in tasks)),),
        tasks=tasks,
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
        project_verification_spec=ProjectVerificationSpec(
            "PROJECT", verification_commands
        ),
    )


def run_scenario(*, real_worker: bool, rework: bool) -> dict[str, object]:
    with TemporaryDirectory(prefix="code-mule-git-e2e-") as directory:
        root = Path(directory)
        repository = root / "repository"
        repository.mkdir()
        git(repository, "init", "-q")
        git(repository, "config", "user.name", "Code Mule E2E")
        git(repository, "config", "user.email", "code-mule@example.invalid")
        (repository / ".gitignore").write_text(
            "__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8"
        )
        (repository / "README.md").write_text("Git delivery E2E\n", encoding="utf-8")
        git(repository, "add", "--", ".gitignore", "README.md")
        git(repository, "commit", "-q", "-m", "initial")

        store = JsonProjectStateStore(root / "project-state.json")
        store.save(initial_state(repository, rework=rework))
        supervisor = FakeSupervisor(repository, rework=rework)
        report_ids = IdFactory("report")
        decision_ids = IdFactory("decision")
        task_event_ids = IdFactory("task-event")
        execution_event_ids = IdFactory("execution-event")

        worker_config = CodexWorkerConfig(
            command=("codex", "app-server"),
            workspace=repository,
            approval_policy="on-request",
            sandbox="workspace-write",
            read_timeout_seconds=360,
        )

        def cycle():
            return TaskCycleService(
                worker_session_factory=(
                    (lambda: CodexWorkerSession(worker_config))
                    if real_worker
                    else (lambda: FakeWorkerSession(repository))
                ),
                supervisor=supervisor,
                store=store,
                clock=lambda: datetime.now(UTC),
                report_id_factory=report_ids,
                decision_id_factory=decision_ids,
                event_id_factory=task_event_ids,
                config=TaskCycleConfig(max_attempts=2),
                git_delivery=GitDeliveryService(
                    repository, clock=lambda: datetime.now(UTC)
                ),
            )

        outcome = ProjectExecutionService(
            store=store,
            scheduler=TaskScheduler(),
            task_cycle_factory=cycle,
            prompt_builder=TaskPromptBuilder(),
            clock=lambda: datetime.now(UTC),
            event_id_factory=execution_event_ids,
            config=ProjectExecutionConfig(max_tasks_per_run=10),
            finalizer=ProjectFinalizationService(
                store=store,
                verification=ProjectVerificationService(
                    clock=lambda: datetime.now(UTC),
                    result_id_factory=IdFactory("verification"),
                ),
                supervisor=supervisor,
                clock=lambda: datetime.now(UTC),
                event_id_factory=IdFactory("verification-event"),
            ),
        ).run()
        final = store.load()
        pending_actions = [
            {
                "category": action.category.value,
                "summary": action.summary,
            }
            for action in final.human_actions
            if action.status.value == "pending"
        ]
        failure_types = [
            event.metadata.get("error_type")
            for event in final.events
            if "error_type" in event.metadata
        ]
        reports = [
            {
                "status": report.status,
                "files_changed": list(report.files_changed),
                "tests": list(report.tests),
                "static_checks": list(report.static_checks),
                "git_state": report.git_state,
                "issue_count": len(report.issues),
            }
            for report in final.execution_reports
        ]
        verification = (
            None
            if not final.project_verification_results
            else final.project_verification_results[-1]
        )
        generated_test = "not_applicable"
        if not rework:
            completed = subprocess.run(
                (sys.executable, "-m", "unittest", "discover", "-s", ".", "-v"),
                cwd=repository,
                text=True,
                capture_output=True,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
            )
            generated_test = "pass" if completed.returncode == 0 else "fail"
        commits = git(repository, "log", "--format=%H%x09%s").splitlines()
        return {
            "worker": "real_codex" if real_worker else "fake",
            "scenario": "rework" if rework else "two_tasks",
            "project_status": final.project.status.value,
            "stop_reason": outcome.stop_reason.value,
            "task_statuses": [task.status.value for task in final.tasks],
            "delivery_commits": [result.commit_sha for result in final.git_commit_results],
            "commit_subjects": [
                line.split("\t", 1)[1] for line in reversed(commits[:-1])
            ],
            "commit_count": len(commits) - 1,
            "review_commit_counts": supervisor.commit_counts_at_review,
            "workspace_clean": git(repository, "status", "--short") == "",
            "generated_tests": generated_test,
            "pending_actions": pending_actions,
            "failure_types": failure_types,
            "worker_reports": reports,
            "verification_checks": (
                []
                if verification is None
                else [
                    {"name": check.name, "status": check.status.value}
                    for check in verification.checks
                ]
            ),
            "final_review_decision": (
                None
                if verification is None or verification.final_review_decision is None
                else verification.final_review_decision.value
            ),
            "final_git_status": git(repository, "status", "--short"),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fake-worker",
        action="store_true",
        help="use a deterministic fake Worker for automated regression",
    )
    parser.add_argument(
        "--scenario", choices=("all", "two_tasks", "rework"), default="all"
    )
    arguments = parser.parse_args()
    payload = {}
    if arguments.scenario in {"all", "two_tasks"}:
        payload["two_tasks"] = run_scenario(
            real_worker=not arguments.fake_worker, rework=False
        )
    if arguments.scenario in {"all", "rework"}:
        payload["rework"] = run_scenario(
            real_worker=not arguments.fake_worker, rework=True
        )
    print(json.dumps(payload, sort_keys=True))
    valid = True
    if "two_tasks" in payload:
        valid = valid and (
            payload["two_tasks"]["project_status"] == "done"
            and payload["two_tasks"]["commit_count"] == 2
            and payload["two_tasks"]["commit_subjects"]
            == ["feat(task): Add calculator addition", "feat(task): Add calculator tests"]
            and payload["two_tasks"]["workspace_clean"]
            and payload["two_tasks"]["generated_tests"] == "pass"
        )
    if "rework" in payload:
        valid = valid and (
            payload["rework"]["project_status"] == "done"
            and payload["rework"]["commit_count"] == 1
            and payload["rework"]["review_commit_counts"] == [1, 1]
            and payload["rework"]["workspace_clean"]
        )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
