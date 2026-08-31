"""Boss-only, billable real DeepSeek + Codex multi-task project E2E."""

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for path in (_ROOT, _SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from openai import DefaultHttpx2Client, OpenAI  # noqa: E402

from code_mule.progress import ConsoleProgressRenderer  # noqa: E402
from code_mule.runtime import (  # noqa: E402
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.supervisor.providers.deepseek import (  # noqa: E402
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService  # noqa: E402
from code_mule.worker import CodexWorkerConfig, CodexWorkerSession  # noqa: E402
from scripts.local_codex_project_smoke import build_project_state  # noqa: E402


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


def _build_supervisor_config(model: str) -> DeepSeekSupervisorConfig:
    return DeepSeekSupervisorConfig(model=model, max_output_tokens=None)


def main() -> int:
    print("REAL DEEPSEEK + CODEX MULTI-TASK PROJECT — MANUAL ONLY")
    print("This makes real DeepSeek API calls and may incur billing.")
    print("This uses the real locally authenticated Codex app-server.")
    print("Codex runs only against a disposable temporary Git repository.")
    print("No production repository, push, tag, release, or deployment is used.")
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    model = os.getenv("CODE_MULE_DEEPSEEK_MODEL")
    if not deepseek_api_key:
        print("ERROR: DEEPSEEK_API_KEY is required.", file=sys.stderr)
        return 2
    if not model:
        print("ERROR: CODE_MULE_DEEPSEEK_MODEL is required.", file=sys.stderr)
        return 2

    supervisor = SupervisorService(
        DeepSeekSupervisorModelClient(
            _build_compatibility_client(deepseek_api_key),
            _build_supervisor_config(model),
        )
    )
    with TemporaryDirectory(prefix="code-mule-manual-project-") as temporary:
        root = Path(temporary)
        workspace = root / "repository"
        workspace.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        store = JsonProjectStateStore(root / "project-state.json")
        store.save(build_project_state(datetime.now(UTC)))
        report_ids = _IdFactory("manual-project-report")
        decision_ids = _IdFactory("manual-project-decision")
        cycle_event_ids = _IdFactory("manual-project-cycle-event")
        progress_clock = lambda: datetime.now(UTC)
        renderer = ConsoleProgressRenderer()

        def task_cycle_factory() -> TaskCycleService:
            def worker_session_factory() -> CodexWorkerSession:
                return CodexWorkerSession(
                    CodexWorkerConfig(
                        command=("codex", "app-server"),
                        workspace=workspace,
                        approval_policy="on-request",
                        sandbox="workspace-write",
                        read_timeout_seconds=360,
                    ),
                    progress_sink=renderer,
                    clock=progress_clock,
                )

            return TaskCycleService(
                worker_session_factory=worker_session_factory,
                supervisor=supervisor,
                store=store,
                clock=progress_clock,
                report_id_factory=report_ids,
                decision_id_factory=decision_ids,
                event_id_factory=cycle_event_ids,
                config=TaskCycleConfig(max_attempts=2),
                progress_sink=renderer,
            )

        runtime = ProjectExecutionService(
            store=store,
            scheduler=TaskScheduler(),
            task_cycle_factory=task_cycle_factory,
            prompt_builder=TaskPromptBuilder(),
            clock=progress_clock,
            event_id_factory=_IdFactory("manual-project-event"),
            config=ProjectExecutionConfig(max_tasks_per_run=2),
            progress_sink=renderer,
        )
        with renderer:
            outcome = runtime.run()
        final_state = store.load()
        print(
            {
                "task_order": outcome.task_ids,
                "tasks_completed": outcome.tasks_completed,
                "stop_reason": outcome.stop_reason.value,
                "project_status": final_state.project.status.value,
                "plan_status": final_state.plans[0].status.value,
                "human_action_required": outcome.human_action_required,
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
