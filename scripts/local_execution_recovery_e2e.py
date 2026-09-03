"""Real local Codex recovery E2E with a deterministic fake Supervisor."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
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

from code_mule.domain import (  # noqa: E402
    Milestone,
    Plan,
    Project,
    ProjectStatus,
    Requirement,
    RequirementStatus,
    SupervisorDecisionType,
    Task,
    TaskStatus,
)
from code_mule.domain.enums import PlanStatus  # noqa: E402
from code_mule.execution import (  # noqa: E402
    ExecutionAlreadyOwned,
    ExecutionLease,
    ExecutionLeaseStatus,
    ExecutionRecoveryRequired,
    RecoveryClassification,
)
from code_mule.execution.service import ExecutionOwnershipService  # noqa: E402
from code_mule.runtime import (  # noqa: E402
    ProjectExecutionConfig,
    ProjectExecutionService,
    ProjectExecutionStopReason,
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
from scripts.local_codex_autonomous_smoke import _IdFactory  # noqa: E402


class _FakeSupervisor:
    def __init__(self) -> None:
        self.review_task_ids: list[str] = []

    def review(self, request: object) -> ReviewResult:
        task = request.task  # type: ignore[attr-defined]
        self.review_task_ids.append(task.id)
        return ReviewResult(
            SupervisorDecisionType.CONTINUE,
            "Deterministic local recovery policy accepted the Worker evidence.",
            None,
            (),
        )


def _repository(root: Path) -> Path:
    workspace = root / "repository"
    workspace.mkdir(parents=True)
    subprocess.run(
        ["git", "init"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return workspace


def _running_state(
    workspace: Path,
    now: datetime,
    *,
    task_in_progress: bool = False,
    lease: ExecutionLease | None = None,
) -> ProjectState:
    project_id = f"recovery-{workspace.parent.name}"
    task_id = "TASK-RECOVERY-PROBE"
    plan_id = "PLAN-RECOVERY-1"
    requirement_id = "REQ-RECOVERY-PROBE"
    task_status = TaskStatus.IN_PROGRESS if task_in_progress else TaskStatus.PENDING
    return ProjectState(
        project=Project(
            project_id,
            "Execution recovery probe",
            ProjectStatus.RUNNING,
            plan_id,
            task_id if task_in_progress else None,
            now,
            now,
            str(workspace),
        ),
        requirements=(
            Requirement(
                requirement_id,
                project_id,
                "Recovery probe",
                "Create a deterministic local file and verify its content.",
                RequirementStatus.ACTIVE,
                "high",
                ("recovery_probe.py returns the expected marker",),
                "phase-14-e2e",
                now,
                now,
            ),
        ),
        plans=(
            Plan(
                plan_id,
                project_id,
                1,
                PlanStatus.ACTIVE,
                (requirement_id,),
                ("M-RECOVERY",),
                now,
            ),
        ),
        milestones=(
            Milestone(
                "M-RECOVERY",
                plan_id,
                "Recovery verification",
                "active",
                (task_id,),
            ),
        ),
        tasks=(
            Task(
                task_id,
                "M-RECOVERY",
                "Create recovery probe",
                "Create recovery_probe.py with marker() returning 'recovered'. "
                "Run a focused Python check; do not push, tag, or use the network.",
                task_status,
                (),
                (
                    "recovery_probe.py exists",
                    "marker() returns recovered",
                ),
                1 if task_in_progress else 0,
                now,
                now,
                (requirement_id,),
            ),
        ),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
        execution_leases=() if lease is None else (lease,),
    )


def _ownership(
    root: Path,
    store: JsonProjectStateStore,
    prefix: str,
) -> ExecutionOwnershipService:
    return ExecutionOwnershipService(
        store=store,
        lock_path=root / ".code-mule" / "execution.lock",
        clock=lambda: datetime.now(UTC),
        lease_id_factory=_IdFactory(f"{prefix}-lease"),
        owner_id_factory=_IdFactory(f"{prefix}-owner"),
        event_id_factory=_IdFactory(f"{prefix}-event"),
        heartbeat_interval_seconds=15,
        stale_after=timedelta(seconds=60),
    )


def _execute_with_real_codex(
    store: JsonProjectStateStore,
    workspace: Path,
    ownership,
    sessions: list[CodexWorkerSession],
):
    clock = lambda: datetime.now(UTC)
    supervisor = _FakeSupervisor()
    worker_config = CodexWorkerConfig(
        command=("codex", "app-server"),
        workspace=workspace,
        approval_policy="on-request",
        sandbox="workspace-write",
        read_timeout_seconds=360,
    )

    def task_cycle_factory() -> TaskCycleService:
        def worker_session_factory() -> CodexWorkerSession:
            session = CodexWorkerSession(worker_config, clock=clock)
            sessions.append(session)
            return session

        return TaskCycleService(
            worker_session_factory=worker_session_factory,
            supervisor=supervisor,
            store=store,
            clock=clock,
            report_id_factory=_IdFactory("recovery-report"),
            decision_id_factory=_IdFactory("recovery-decision"),
            event_id_factory=_IdFactory("recovery-cycle-event"),
            config=TaskCycleConfig(max_attempts=1),
            worker_identity_started=ownership.record_worker_identity,
            worker_identity_cleared=ownership.clear_worker_identity,
        )

    execution = ProjectExecutionService(
        store=store,
        scheduler=TaskScheduler(),
        task_cycle_factory=task_cycle_factory,
        prompt_builder=TaskPromptBuilder(),
        clock=clock,
        event_id_factory=_IdFactory("recovery-project-event"),
        config=ProjectExecutionConfig(max_tasks_per_run=1),
    )
    return execution.run(), supervisor


def _scenario_a(root: Path) -> dict[str, object]:
    workspace = _repository(root)
    store = JsonProjectStateStore(root / "project-state.json")
    store.save(_running_state(workspace, datetime.now(UTC)))
    sessions: list[CodexWorkerSession] = []
    second_blocked = False
    with _ownership(root, store, "a").acquire() as owner:
        try:
            _ownership(root, store, "b").acquire()
        except ExecutionAlreadyOwned:
            second_blocked = True
        duplicate_prevented_before_worker = second_blocked and sessions == []
        outcome, supervisor = _execute_with_real_codex(
            store, workspace, owner, sessions
        )
    final = store.load()
    return {
        "second_blocked": second_blocked,
        "duplicate_prevented_before_worker": duplicate_prevented_before_worker,
        "session_count": len(sessions),
        "review_count": len(supervisor.review_task_ids),
        "stop_reason": outcome.stop_reason.value,
        "final_status": final.project.status.value,
        "lease_status": final.execution_leases[-1].status.value,
        "failure_types": [
            event.metadata["error_type"]
            for event in final.events
            if "error_type" in event.metadata
        ],
        "human_action_categories": [
            action.category.value for action in final.human_actions
        ],
    }


def _scenario_b(root: Path) -> dict[str, object]:
    workspace = _repository(root)
    now = datetime.now(UTC)
    project_id = f"recovery-{root.name}"
    stale = ExecutionLease(
        "stale-boundary",
        project_id,
        "dead-owner",
        os.getpid(),
        now - timedelta(minutes=5),
        now - timedelta(minutes=5),
        ExecutionLeaseStatus.ACTIVE,
    )
    store = JsonProjectStateStore(root / "project-state.json")
    store.save(_running_state(workspace, now, lease=stale))
    sessions: list[CodexWorkerSession] = []
    with _ownership(root, store, "recovered").acquire() as owner:
        outcome, _ = _execute_with_real_codex(store, workspace, owner, sessions)
    final = store.load()
    return {
        "old_lease_status": final.execution_leases[0].status.value,
        "new_lease_status": final.execution_leases[1].status.value,
        "recovered_event": any(
            event.event_type == "execution.recovered" for event in final.events
        ),
        "session_count": len(sessions),
        "stop_reason": outcome.stop_reason.value,
        "final_status": final.project.status.value,
        "failure_types": [
            event.metadata["error_type"]
            for event in final.events
            if "error_type" in event.metadata
        ],
        "human_action_categories": [
            action.category.value for action in final.human_actions
        ],
    }


def _scenario_c(root: Path) -> dict[str, object]:
    workspace = _repository(root)
    now = datetime.now(UTC)
    project_id = f"recovery-{root.name}"
    stale = ExecutionLease(
        "stale-in-progress",
        project_id,
        "dead-owner",
        999999,
        now - timedelta(minutes=5),
        now - timedelta(minutes=5),
        ExecutionLeaseStatus.ACTIVE,
        "TASK-RECOVERY-PROBE",
        "codex-thread-from-dead-owner",
        1,
    )
    store = JsonProjectStateStore(root / "project-state.json")
    store.save(
        _running_state(workspace, now, task_in_progress=True, lease=stale)
    )
    classification = None
    try:
        _ownership(root, store, "unsafe").acquire()
    except ExecutionRecoveryRequired as error:
        classification = error.decision.classification.value
    final = store.load()
    return {
        "classification": classification,
        "final_status": final.project.status.value,
        "pending_recovery_actions": sum(
            action.category.value == "recovery_uncertain"
            and action.status.value == "pending"
            for action in final.human_actions
        ),
        "active_new_lease_count": sum(
            lease.status is ExecutionLeaseStatus.ACTIVE
            for lease in final.execution_leases
        ),
        "worker_session_count": 0,
    }


def main() -> int:
    with TemporaryDirectory(prefix="code-mule-recovery-local-") as temporary:
        root = Path(temporary)
        scenario_a = _scenario_a(root / "scenario-a")
        scenario_b = _scenario_b(root / "scenario-b")
        scenario_c = _scenario_c(root / "scenario-c")
        payload = {
            "workspace_type": "disposable temporary git repositories",
            "supervisor": "deterministic fake",
            "worker": "real local Codex app-server",
            "scenario_a": scenario_a,
            "scenario_b": scenario_b,
            "scenario_c": scenario_c,
        }
        print(json.dumps(payload, sort_keys=True))
        passed = (
            scenario_a["second_blocked"] is True
            and scenario_a["duplicate_prevented_before_worker"] is True
            and scenario_a["session_count"] == 1
            and scenario_a["review_count"] == 1
            and scenario_a["stop_reason"]
            == ProjectExecutionStopReason.PLAN_COMPLETED.value
            and scenario_a["final_status"] == ProjectStatus.DONE.value
            and scenario_a["lease_status"] == ExecutionLeaseStatus.RELEASED.value
            and scenario_b["old_lease_status"] == ExecutionLeaseStatus.STALE.value
            and scenario_b["new_lease_status"]
            == ExecutionLeaseStatus.RELEASED.value
            and scenario_b["recovered_event"] is True
            and scenario_b["session_count"] == 1
            and scenario_b["stop_reason"]
            == ProjectExecutionStopReason.PLAN_COMPLETED.value
            and scenario_b["final_status"] == ProjectStatus.DONE.value
            and scenario_c["classification"]
            == RecoveryClassification.SESSION_RECOVERY_REQUIRED.value
            and scenario_c["final_status"] == ProjectStatus.HUMAN_REQUIRED.value
            and scenario_c["pending_recovery_actions"] == 1
            and scenario_c["active_new_lease_count"] == 0
            and scenario_c["worker_session_count"] == 0
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
