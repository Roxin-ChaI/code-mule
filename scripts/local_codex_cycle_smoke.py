"""Real local Codex + fake Supervisor smoke test in a disposable repository."""

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_mule.domain.enums import (  # noqa: E402
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import Project, Task  # noqa: E402
from code_mule.runtime import (  # noqa: E402
    TaskCycleConfig,
    TaskCycleRequest,
    TaskCycleService,
)
from code_mule.state.models import ProjectState  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor.contracts import ReviewResult  # noqa: E402
from code_mule.worker import (  # noqa: E402
    CodexWorkerConfig,
    CodexWorkerSession,
)


class _IdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._prefix}-{self._value}"


class _ReworkThenContinueSupervisor:
    def __init__(self) -> None:
        self.review_count = 0

    def review(self, request: object) -> ReviewResult:
        self.review_count += 1
        if self.review_count == 1:
            return ReviewResult(
                decision=SupervisorDecisionType.REWORK,
                rationale="Force a second turn to verify same-thread context.",
                next_task_prompt=(
                    "Re-check calculator.py and test_calculator.py, run the local "
                    "unit test again, and correct any remaining problem. Do not push, "
                    "tag, or access external services."
                ),
                issues=(),
            )
        return ReviewResult(
            decision=SupervisorDecisionType.CONTINUE,
            rationale="The fixed fixture has been checked through two Worker turns.",
            next_task_prompt=None,
            issues=(),
        )


def _initial_state(now: datetime) -> ProjectState:
    task = Task(
        id="local-codex-cycle-task",
        milestone_id="local-smoke",
        title="Fix calculator addition",
        description="Correct add() and verify it locally.",
        status=TaskStatus.IN_PROGRESS,
        dependencies=(),
        acceptance_criteria=("add returns a + b", "local unit test passes"),
        execution_attempts=0,
        created_at=now,
        updated_at=now,
    )
    project = Project(
        id="local-codex-cycle-project",
        name="Disposable local Codex cycle",
        status=ProjectStatus.RUNNING,
        active_plan_id=None,
        current_task_id=task.id,
        created_at=now,
        updated_at=now,
    )
    return ProjectState(
        project=project,
        requirements=(),
        plans=(),
        milestones=(),
        tasks=(task,),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


def _prepare_repository(workspace: Path) -> None:
    workspace.mkdir()
    (workspace / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (workspace / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add\n\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> int:
    with TemporaryDirectory(prefix="code-mule-local-cycle-") as temporary:
        root = Path(temporary)
        workspace = root / "repository"
        _prepare_repository(workspace)
        now = datetime.now(UTC)
        store = JsonProjectStateStore(root / "project-state.json")
        initial = _initial_state(now)
        store.save(initial)
        supervisor = _ReworkThenContinueSupervisor()
        sessions: list[CodexWorkerSession] = []

        def session_factory() -> CodexWorkerSession:
            session = CodexWorkerSession(
                CodexWorkerConfig(
                    command=("codex", "app-server"),
                    workspace=workspace,
                    approval_policy="on-request",
                    sandbox="workspace-write",
                    inactivity_timeout_seconds=120,
                    max_turn_seconds=900,
                )
            )
            sessions.append(session)
            return session

        cycle = TaskCycleService(
            worker_session_factory=session_factory,
            supervisor=supervisor,
            store=store,
            clock=lambda: datetime.now(UTC),
            report_id_factory=_IdFactory("local-report"),
            decision_id_factory=_IdFactory("local-decision"),
            event_id_factory=_IdFactory("local-event"),
            config=TaskCycleConfig(max_attempts=2),
        )
        outcome = cycle.execute(
            TaskCycleRequest(
                task=initial.tasks[0],
                initial_prompt=(
                    "Fix calculator.py so add(a, b) returns the sum. Run "
                    "test_calculator.py locally. Do not push, tag, or access "
                    "external services."
                ),
            )
        )
        final_state = store.load()
        git_status = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        verification = subprocess.run(
            [sys.executable, "-m", "unittest", "-v"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        payload = {
            "workspace_type": "disposable temporary git repository",
            "thread_id": sessions[0].thread_id,
            "session_count": len(sessions),
            "review_count": supervisor.review_count,
            "attempts": outcome.attempts,
            "final_decision": outcome.final_decision.value,
            "task_status": final_state.tasks[0].status.value,
            "task_execution_attempts": final_state.tasks[0].execution_attempts,
            "reports": [
                {
                    "id": report.id,
                    "attempt": report.attempt,
                    "status": report.status,
                    "files_changed": report.files_changed,
                    "tests": report.tests,
                    "static_checks": report.static_checks,
                    "git_state": report.git_state,
                    "human_action_required": report.human_action_required,
                }
                for report in outcome.execution_reports
            ],
            "cycle_events": [
                {
                    "type": event.event_type,
                    "metadata": event.metadata,
                }
                for event in final_state.events
            ],
            "calculator": (workspace / "calculator.py").read_text(encoding="utf-8"),
            "git_status": git_status,
            "independent_test_returncode": verification.returncode,
        }
        print(json.dumps(payload, sort_keys=True))
        passed = (
            outcome.final_decision is SupervisorDecisionType.CONTINUE
            and outcome.attempts == 2
            and len(sessions) == 1
            and supervisor.review_count == 2
            and final_state.tasks[0].status is TaskStatus.COMPLETED
            and final_state.tasks[0].execution_attempts == 2
            and (workspace / "calculator.py").read_text(encoding="utf-8")
            == "def add(a, b):\n    return a + b\n"
            and verification.returncode == 0
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
