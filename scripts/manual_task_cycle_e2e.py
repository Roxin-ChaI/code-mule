"""Boss-only, billable real DeepSeek + Codex task-cycle E2E."""

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openai import DefaultHttpx2Client, OpenAI  # noqa: E402

from code_mule.domain.enums import ProjectStatus, TaskStatus  # noqa: E402
from code_mule.domain.models import Project, Task  # noqa: E402
from code_mule.runtime import (  # noqa: E402
    TaskCycleConfig,
    TaskCycleRequest,
    TaskCycleService,
)
from code_mule.state.models import ProjectState  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor.providers.deepseek import (  # noqa: E402
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService  # noqa: E402
from code_mule.worker import CodexWorkerConfig, CodexWorkerSession  # noqa: E402


class _IdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._prefix}-{self._value}"


def _build_compatibility_client(deepseek_api_key: str) -> OpenAI:
    return OpenAI(
        api_key=deepseek_api_key,
        base_url="https://api.deepseek.com",
        max_retries=0,
        http_client=DefaultHttpx2Client(trust_env=False),
    )


def _initial_state(now: datetime) -> ProjectState:
    task = Task(
        id="manual-task-cycle-task",
        milestone_id="manual-e2e",
        title="Fix calculator addition",
        description="Correct add() in a disposable repository and verify it.",
        status=TaskStatus.IN_PROGRESS,
        dependencies=(),
        acceptance_criteria=("add returns the sum", "local test passes"),
        execution_attempts=0,
        created_at=now,
        updated_at=now,
    )
    return ProjectState(
        project=Project(
            id="manual-task-cycle-project",
            name="Manual DeepSeek and Codex cycle",
            status=ProjectStatus.RUNNING,
            active_plan_id=None,
            current_task_id=task.id,
            created_at=now,
            updated_at=now,
        ),
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


def main() -> int:
    print("REAL DEEPSEEK + CODEX TASK CYCLE — MANUAL ONLY")
    print("This makes real DeepSeek API calls and may incur billing.")
    print("Codex runs only against a disposable temporary repository.")
    print("No production repository, push, tag, release, or deployment is used.")
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    model = os.getenv("CODE_MULE_DEEPSEEK_MODEL")
    if not deepseek_api_key:
        print("ERROR: DEEPSEEK_API_KEY is required.", file=sys.stderr)
        return 2
    if not model:
        print("ERROR: CODE_MULE_DEEPSEEK_MODEL is required.", file=sys.stderr)
        return 2

    compatibility_client = _build_compatibility_client(deepseek_api_key)
    supervisor = SupervisorService(
        DeepSeekSupervisorModelClient(
            compatibility_client,
            DeepSeekSupervisorConfig(model=model, max_output_tokens=1000),
        )
    )
    with TemporaryDirectory(prefix="code-mule-manual-cycle-") as temporary:
        root = Path(temporary)
        workspace = root / "repository"
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
        now = datetime.now(UTC)
        initial = _initial_state(now)
        store = JsonProjectStateStore(root / "project-state.json")
        store.save(initial)

        def session_factory() -> CodexWorkerSession:
            return CodexWorkerSession(
                CodexWorkerConfig(
                    command=("codex", "app-server"),
                    workspace=workspace,
                    approval_policy="on-request",
                    sandbox="workspace-write",
                    read_timeout_seconds=360,
                )
            )

        cycle = TaskCycleService(
            worker_session_factory=session_factory,
            supervisor=supervisor,
            store=store,
            clock=lambda: datetime.now(UTC),
            report_id_factory=_IdFactory("manual-report"),
            decision_id_factory=_IdFactory("manual-decision"),
            event_id_factory=_IdFactory("manual-event"),
            config=TaskCycleConfig(max_attempts=2),
        )
        outcome = cycle.execute(
            TaskCycleRequest(
                task=initial.tasks[0],
                initial_prompt=(
                    "Fix calculator.py so add(a, b) returns the sum, then run "
                    "test_calculator.py locally. Work only in this repository. "
                    "Do not push, tag, release, deploy, or access external services."
                ),
            )
        )
        print(
            {
                "task_id": outcome.task_id,
                "attempts": outcome.attempts,
                "final_decision": outcome.final_decision.value,
                "human_action_required": outcome.human_action_required,
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
