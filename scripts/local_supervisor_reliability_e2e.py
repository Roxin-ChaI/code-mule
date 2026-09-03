"""Deterministic local E2E for bounded Supervisor regeneration."""

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for path in (_ROOT, _SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from code_mule.domain import (  # noqa: E402
    ExecutionReport,
    Project,
    ProjectStatus,
    SupervisorDecisionType,
)
from code_mule.planning import (  # noqa: E402
    InvalidPlanProposal,
    ProjectPlanningRequest,
    ProjectPlanningService,
    SupervisorPlanningError,
)
from code_mule.progress import (  # noqa: E402
    ProgressEventType,
    RecordingProgressSink,
)
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
from code_mule.supervisor import (  # noqa: E402
    SupervisorOperation,
    SupervisorService,
)
from scripts.local_codex_autonomous_smoke import _IdFactory  # noqa: E402


def _plan_payload(*, requirement_id: str = "REQ-LOCAL") -> dict[str, object]:
    return {
        "summary": "Create one deterministic local probe.",
        "requirements": [
            {
                "id": "REQ-LOCAL",
                "title": "Local probe",
                "description": "Produce one locally verified probe.",
                "priority": "high",
                "acceptance_criteria": ["probe completes"],
            }
        ],
        "requirements_considered": [],
        "milestones": [
            {
                "id": "M-LOCAL",
                "title": "Local reliability",
                "task_ids": ["TASK-LOCAL"],
            }
        ],
        "tasks": [
            {
                "id": "TASK-LOCAL",
                "title": "Run local probe",
                "description": "Run the deterministic fake Worker once.",
                "dependencies": [],
                "acceptance_criteria": ["fake Worker reports completion"],
                "requirement_ids": [requirement_id],
            }
        ],
        "risks": [],
        "rationale": "One task isolates Supervisor retry behavior.",
    }


def _review_payload(*, invalid_combination: bool = False) -> dict[str, object]:
    if invalid_combination:
        return {
            "decision": "human_required",
            "rationale": "Invalid fake response.",
            "next_task_prompt": "must not be accepted",
            "issues": [],
        }
    return {
        "decision": "continue",
        "rationale": "Deterministic fake evidence accepted.",
        "next_task_prompt": None,
        "issues": [],
    }


class _FakeModelClient:
    def __init__(self, outcomes: dict[SupervisorOperation, list[object]]) -> None:
        self.outcomes = {key: list(values) for key, values in outcomes.items()}
        self.calls: list[SupervisorOperation] = []

    def create_structured_response(self, *, operation, **_):
        self.calls.append(operation)
        outcome = self.outcomes[operation].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeWorkerSession:
    def __init__(self, executions: list[str]) -> None:
        self.executions = executions

    @property
    def thread_id(self) -> str:
        return "fake-local-thread"

    def start(self) -> None:
        return None

    def execute(self, request, *, report_id, created_at):
        self.executions.append(request.task.id)
        return ExecutionReport(
            report_id,
            request.task.id,
            request.task.execution_attempts + 1,
            "completed",
            (),
            ("deterministic fake check: pass",),
            (),
            "clean",
            (),
            False,
            "Fake Worker completed once.",
            created_at,
        )

    def close(self) -> None:
        return None


def _empty_state(workspace: Path, project_id: str) -> ProjectState:
    now = datetime.now(UTC)
    return ProjectState(
        project=Project(
            project_id,
            "Supervisor reliability local E2E",
            ProjectStatus.IDLE,
            None,
            None,
            now,
            now,
            str(workspace),
        ),
        requirements=(),
        plans=(),
        milestones=(),
        tasks=(),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


def _planning(
    store: JsonProjectStateStore,
    supervisor: SupervisorService,
    progress: RecordingProgressSink,
    prefix: str,
) -> ProjectPlanningService:
    return ProjectPlanningService(
        store=store,
        supervisor=supervisor,
        clock=lambda: datetime.now(UTC),
        plan_id_factory=lambda: f"{prefix}-PLAN-1",
        event_id_factory=_IdFactory(f"{prefix}-planning-event"),
        progress_sink=progress,
    )


def _execution(
    store: JsonProjectStateStore,
    supervisor: SupervisorService,
    progress: RecordingProgressSink,
    worker_executions: list[str],
    worker_sessions: list[_FakeWorkerSession],
    prefix: str,
) -> ProjectExecutionService:
    def task_cycle_factory() -> TaskCycleService:
        def worker_session_factory() -> _FakeWorkerSession:
            session = _FakeWorkerSession(worker_executions)
            worker_sessions.append(session)
            return session

        return TaskCycleService(
            worker_session_factory=worker_session_factory,
            supervisor=supervisor,
            store=store,
            clock=lambda: datetime.now(UTC),
            report_id_factory=_IdFactory(f"{prefix}-report"),
            decision_id_factory=_IdFactory(f"{prefix}-decision"),
            event_id_factory=_IdFactory(f"{prefix}-cycle-event"),
            config=TaskCycleConfig(max_attempts=1),
            progress_sink=progress,
        )

    return ProjectExecutionService(
        store=store,
        scheduler=TaskScheduler(),
        task_cycle_factory=task_cycle_factory,
        prompt_builder=TaskPromptBuilder(),
        clock=lambda: datetime.now(UTC),
        event_id_factory=_IdFactory(f"{prefix}-execution-event"),
        config=ProjectExecutionConfig(max_tasks_per_run=1),
        progress_sink=progress,
    )


def _scenario_a(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    store = JsonProjectStateStore(root / "state.json")
    store.save(_empty_state(workspace, "reliability-a"))
    progress = RecordingProgressSink()
    client = _FakeModelClient(
        {
            SupervisorOperation.PLAN: [_plan_payload()],
            SupervisorOperation.REVIEW: [
                _review_payload(invalid_combination=True),
                _review_payload(),
            ],
        }
    )
    supervisor = SupervisorService(client, progress_sink=progress)
    planning = _planning(store, supervisor, progress, "a")
    planning.plan(ProjectPlanningRequest("reliability-a", "Run local probe"))
    worker_executions: list[str] = []
    worker_sessions: list[_FakeWorkerSession] = []
    outcome = _execution(
        store,
        supervisor,
        progress,
        worker_executions,
        worker_sessions,
        "a",
    ).run()
    final = store.load()
    progress_types = tuple(event.type for event in progress.events)
    return {
        "project_status": final.project.status.value,
        "stop_reason": outcome.stop_reason.value,
        "plan_calls": client.calls.count(SupervisorOperation.PLAN),
        "review_calls": client.calls.count(SupervisorOperation.REVIEW),
        "worker_sessions": len(worker_sessions),
        "worker_executions": len(worker_executions),
        "retrying": progress_types.count(
            ProgressEventType.SUPERVISOR_RETRYING
        ),
        "retry_succeeded": progress_types.count(
            ProgressEventType.SUPERVISOR_RETRY_SUCCEEDED
        ),
    }


def _scenario_b(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    store = JsonProjectStateStore(root / "state.json")
    store.save(_empty_state(workspace, "reliability-b"))
    progress = RecordingProgressSink()
    client = _FakeModelClient(
        {
            SupervisorOperation.PLAN: [
                {"invalid": "first response"},
                {"invalid": "second response"},
            ]
        }
    )
    supervisor = SupervisorService(client, progress_sink=progress)
    failed = False
    try:
        _planning(store, supervisor, progress, "b").plan(
            ProjectPlanningRequest("reliability-b", "Run local probe")
        )
    except SupervisorPlanningError:
        failed = True
    final = store.load()
    failure_event = final.events[-1]
    return {
        "typed_failure": failed,
        "project_status": final.project.status.value,
        "plan_calls": client.calls.count(SupervisorOperation.PLAN),
        "worker_sessions": 0,
        "event_type": failure_event.event_type,
        "attempt_count": failure_event.metadata.get("attempt_count"),
        "failure_category": failure_event.metadata.get("failure_category"),
    }


def _scenario_c(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    store = JsonProjectStateStore(root / "state.json")
    store.save(_empty_state(workspace, "reliability-c"))
    progress = RecordingProgressSink()
    client = _FakeModelClient(
        {SupervisorOperation.PLAN: [_plan_payload(requirement_id="UNKNOWN-REQ")]}
    )
    supervisor = SupervisorService(client, progress_sink=progress)
    rejected = False
    try:
        _planning(store, supervisor, progress, "c").plan(
            ProjectPlanningRequest("reliability-c", "Run local probe")
        )
    except InvalidPlanProposal:
        rejected = True
    final = store.load()
    failure_event = final.events[-1]
    return {
        "proposal_rejected": rejected,
        "project_status": final.project.status.value,
        "plan_calls": client.calls.count(SupervisorOperation.PLAN),
        "worker_sessions": 0,
        "event_type": failure_event.event_type,
        "failure_category": failure_event.metadata.get("failure_category"),
        "retry_events": sum(
            event.type
            in {
                ProgressEventType.SUPERVISOR_RETRYING,
                ProgressEventType.SUPERVISOR_RETRY_SUCCEEDED,
                ProgressEventType.SUPERVISOR_RETRY_EXHAUSTED,
            }
            for event in progress.events
        ),
    }


def main() -> int:
    with TemporaryDirectory(prefix="code-mule-supervisor-reliability-") as temp:
        root = Path(temp)
        scenario_a = _scenario_a(root / "scenario-a")
        scenario_b = _scenario_b(root / "scenario-b")
        scenario_c = _scenario_c(root / "scenario-c")
        payload = {
            "provider": "deterministic fake",
            "scenario_a": scenario_a,
            "scenario_b": scenario_b,
            "scenario_c": scenario_c,
        }
        print(json.dumps(payload, sort_keys=True))
        passed = (
            scenario_a["project_status"] == ProjectStatus.DONE.value
            and scenario_a["stop_reason"]
            == ProjectExecutionStopReason.PLAN_COMPLETED.value
            and scenario_a["plan_calls"] == 1
            and scenario_a["review_calls"] == 2
            and scenario_a["worker_sessions"] == 1
            and scenario_a["worker_executions"] == 1
            and scenario_a["retrying"] == 1
            and scenario_a["retry_succeeded"] == 1
            and scenario_b["typed_failure"] is True
            and scenario_b["project_status"]
            == ProjectStatus.HUMAN_REQUIRED.value
            and scenario_b["plan_calls"] == 2
            and scenario_b["worker_sessions"] == 0
            and scenario_b["event_type"] == "planning.failed"
            and scenario_b["attempt_count"] == "2"
            and scenario_b["failure_category"]
            == "schema_contract_violation"
            and scenario_c["proposal_rejected"] is True
            and scenario_c["project_status"]
            == ProjectStatus.HUMAN_REQUIRED.value
            and scenario_c["plan_calls"] == 1
            and scenario_c["worker_sessions"] == 0
            and scenario_c["event_type"] == "planning.proposal_rejected"
            and scenario_c["failure_category"]
            == "invalid_business_reference"
            and scenario_c["retry_events"] == 0
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
