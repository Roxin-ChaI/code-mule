"""Disposable STOP lifecycle E2E with fake Supervisor and optional real Codex."""

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from code_mule.domain import ProjectStatus  # noqa: E402
from code_mule.git_delivery import GitDeliveryService  # noqa: E402
from code_mule.orchestrator import OrchestratorService, StopCommand  # noqa: E402
from code_mule.runtime import (  # noqa: E402
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402
from code_mule.worker import CodexWorkerConfig, CodexWorkerSession  # noqa: E402

from local_git_delivery_e2e import (  # noqa: E402
    FakeSupervisor,
    FakeWorkerSession,
    IdFactory,
    git,
    initial_state,
)


class StopBeforeWorkerTurn:
    """Submit STOP after session start, then let that one turn finish safely."""

    def __init__(self, delegate, orchestrator, project_id):
        self.delegate = delegate
        self.orchestrator = orchestrator
        self.project_id = project_id
        self.stopped = False

    @property
    def thread_id(self):
        return self.delegate.thread_id

    def start(self):
        return self.delegate.start()

    def execute(self, request, *, report_id, created_at):
        if not self.stopped:
            self.orchestrator.stop(StopCommand(self.project_id))
            self.stopped = True
        return self.delegate.execute(
            request, report_id=report_id, created_at=created_at
        )

    def close(self):
        return self.delegate.close()


class ForbiddenFinalizer:
    def __init__(self):
        self.calls = 0

    def finalize(self, state):
        self.calls += 1
        raise AssertionError("cancelled project must not run final verification")


def active_scenario(root: Path, *, real_worker: bool) -> dict[str, object]:
    repository = root / "active-repository"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Code Mule E2E")
    git(repository, "config", "user.email", "code-mule@example.invalid")
    (repository / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (repository / "README.md").write_text("Cancellation E2E\n", encoding="utf-8")
    git(repository, "add", "--", ".gitignore", "README.md")
    git(repository, "commit", "-q", "-m", "initial")

    store = JsonProjectStateStore(root / "active-state.json")
    store.save(initial_state(repository, rework=False))
    supervisor = FakeSupervisor(repository, rework=False)
    orchestrator = OrchestratorService(
        store,
        clock=lambda: datetime.now(UTC),
        event_id_factory=IdFactory("stop-event"),
    )
    worker_config = CodexWorkerConfig(
        command=("codex", "app-server"),
        workspace=repository,
        approval_policy="on-request",
        sandbox="workspace-write",
        inactivity_timeout_seconds=120,
        max_turn_seconds=900,
    )

    def cycle():
        delegate = (
            CodexWorkerSession(worker_config)
            if real_worker
            else FakeWorkerSession(repository)
        )
        return TaskCycleService(
            worker_session_factory=lambda: StopBeforeWorkerTurn(
                delegate, orchestrator, "PROJECT"
            ),
            supervisor=supervisor,
            store=store,
            clock=lambda: datetime.now(UTC),
            report_id_factory=IdFactory("report"),
            decision_id_factory=IdFactory("decision"),
            event_id_factory=IdFactory("task-event"),
            config=TaskCycleConfig(max_attempts=2),
            git_delivery=GitDeliveryService(
                repository, clock=lambda: datetime.now(UTC)
            ),
        )

    finalizer = ForbiddenFinalizer()
    outcome = ProjectExecutionService(
        store=store,
        scheduler=TaskScheduler(),
        task_cycle_factory=cycle,
        prompt_builder=TaskPromptBuilder(),
        clock=lambda: datetime.now(UTC),
        event_id_factory=IdFactory("runtime-event"),
        config=ProjectExecutionConfig(max_tasks_per_run=10),
        finalizer=finalizer,
    ).run()
    final = store.load()
    return {
        "worker": "real_codex" if real_worker else "fake",
        "project_status": final.project.status.value,
        "stop_reason": outcome.stop_reason.value,
        "task_statuses": [task.status.value for task in final.tasks],
        "task_ids_started": list(outcome.task_ids),
        "commit_count": int(git(repository, "rev-list", "--count", "HEAD")) - 1,
        "delivery_commit_count": len(final.git_commit_results),
        "workspace_clean": git(repository, "status", "--short") == "",
        "finalizer_calls": finalizer.calls,
        "event_types": [event.event_type for event in final.events],
    }


def immediate_scenario(root: Path, status: ProjectStatus) -> dict[str, object]:
    repository = root / status.value
    repository.mkdir()
    store = JsonProjectStateStore(root / f"{status.value}.json")
    state = initial_state(repository, rework=False)
    state = replace(
        state,
        project=replace(state.project, status=status, current_task_id=None),
    )
    store.save(state)
    result = OrchestratorService(
        store,
        clock=lambda: datetime.now(UTC),
        event_id_factory=IdFactory("immediate-event"),
    ).stop(StopCommand(state.project.id))
    final = store.load()
    return {
        "source_status": status.value,
        "project_status": final.project.status.value,
        "safe_point_required": result.safe_point_required,
        "task_statuses": [task.status.value for task in final.tasks],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake-worker", action="store_true")
    arguments = parser.parse_args()
    with TemporaryDirectory(prefix="code-mule-cancellation-e2e-") as directory:
        root = Path(directory)
        payload = {
            "active": active_scenario(root, real_worker=not arguments.fake_worker),
            "idle": immediate_scenario(root, ProjectStatus.RUNNING),
            "human_required": immediate_scenario(root, ProjectStatus.HUMAN_REQUIRED),
        }
    print(json.dumps(payload, sort_keys=True))
    active = payload["active"]
    valid = (
        active["project_status"] == "cancelled"
        and active["stop_reason"] == "cancelled"
        and active["task_statuses"] == ["completed", "cancelled"]
        and active["task_ids_started"] == ["TASK-ADD"]
        and active["commit_count"] == 1
        and active["delivery_commit_count"] == 1
        and active["workspace_clean"]
        and active["finalizer_calls"] == 0
        and payload["idle"]["project_status"] == "cancelled"
        and payload["human_required"]["project_status"] == "cancelled"
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
