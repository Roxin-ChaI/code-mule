import unittest
from dataclasses import replace
from datetime import UTC, datetime

from code_mule.domain.enums import (
    CapabilityApprovalScope,
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
    WorkerHumanActionKind,
)
from code_mule.domain.models import ExecutionReport, WorkerHumanAction
from code_mule.execution import ExecutionLease, ExecutionLeaseStatus
from code_mule.git_delivery import (
    DirtyGitBaseline,
    GitBaseline,
    GitChangeSet,
    GitCommitError,
    GitCommitResult,
    GitNoCommitResult,
)
from code_mule.human import HumanResolutionService
from code_mule.orchestrator import (
    ChangeCommand,
    OrchestratorService,
    PauseCommand,
)
from code_mule.progress import ProgressEventType, RecordingProgressSink
from code_mule.replanning import (
    ChangeReplanningRequest,
    ChangeReplanningService,
    SupervisorReplanningError,
)
from code_mule.runtime import (
    InvalidTaskCycleState,
    TaskCycleConfig,
    TaskCycleRequest,
    TaskCycleService,
)
from code_mule.supervisor.contracts import ReviewResult
from code_mule.worker import (
    CodexCapabilityApprovalRequired,
    CodexApprovalRequired,
    CodexTurnHardTimeout,
    CodexTurnTimeout,
    InvalidWorkerReport,
    WorkerCapabilityApprovalRequest,
)
from code_mule.worker import CodexUserInputRequired, WorkerInputRequest

from tests.state import make_project_state


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def cycle_state():
    source = make_project_state()
    task = replace(
        source.tasks[0],
        status=TaskStatus.IN_PROGRESS,
        execution_attempts=0,
    )
    return replace(
        source,
        project=replace(
            source.project,
            status=ProjectStatus.RUNNING,
            current_task_id=task.id,
        ),
        tasks=(task,),
        decisions=(),
        execution_reports=(),
    )


class FakeStore:
    def __init__(self, state, *, fail_on_save=None):
        self.current = state
        self.saved = []
        self.fail_on_save = fail_on_save

    def load(self):
        return self.current

    def save(self, state):
        save_number = len(self.saved) + 1
        if save_number == self.fail_on_save:
            raise OSError("persistence failed")
        self.saved.append(state)
        self.current = state


class FakeWorkerSession:
    def __init__(self, outcomes=None, *, human_action_required=False):
        self.outcomes = list(outcomes or [])
        self.human_action_required = human_action_required
        self.started = 0
        self.closed = 0
        self.requests = []
        self.thread_marker = object()

    @property
    def thread_id(self):
        return "thread-1"

    def start(self):
        self.started += 1

    def execute(self, request, *, report_id, created_at):
        self.requests.append((request, report_id, created_at, self.thread_marker))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            status = outcome
        else:
            status = "completed"
        return ExecutionReport(
            id=report_id,
            task_id=request.task.id,
            attempt=request.task.execution_attempts + 1,
            status=status,
            files_changed=("calculator.py",),
            tests=("python -m unittest: pass",),
            static_checks=("compileall: pass",),
            git_state="dirty",
            issues=(),
            human_action=(
                WorkerHumanAction(
                    WorkerHumanActionKind.EXTERNAL_SIDE_EFFECT,
                    "External operation requires approval",
                    "Approve the external operation",
                )
                if self.human_action_required
                else None
            ),
            summary=f"attempt {request.task.execution_attempts + 1}",
            created_at=created_at,
        )

    def close(self):
        self.closed += 1


class FakeSupervisor:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class IdFactory:
    def __init__(self, prefix):
        self.prefix = prefix
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"{self.prefix}-{self.count}"


def review(decision, prompt=None, rationale="deterministic review"):
    return ReviewResult(decision, rationale, prompt, ())


def build_cycle(
    *,
    store=None,
    session=None,
    supervisor=None,
    max_attempts=3,
    progress_sink=None,
    worker_identity_started=None,
    worker_identity_cleared=None,
    git_delivery=None,
):
    store = store or FakeStore(cycle_state())
    session = session or FakeWorkerSession()
    supervisor = supervisor or FakeSupervisor(
        [review(SupervisorDecisionType.CONTINUE)]
    )
    sessions = []

    def session_factory():
        sessions.append(session)
        return session

    service = TaskCycleService(
        worker_session_factory=session_factory,
        supervisor=supervisor,
        store=store,
        clock=lambda: NOW,
        report_id_factory=IdFactory("report"),
        decision_id_factory=IdFactory("decision"),
        event_id_factory=IdFactory("event"),
        config=TaskCycleConfig(max_attempts),
        progress_sink=progress_sink,
        worker_identity_started=worker_identity_started,
        worker_identity_cleared=worker_identity_cleared,
        git_delivery=git_delivery,
    )
    request_task = (
        store.current.tasks[0]
        if store.current.tasks
        else cycle_state().tasks[0]
    )
    request = TaskCycleRequest(request_task, "initial prompt")
    return service, request, store, session, supervisor, sessions


class FakeGitDelivery:
    def __init__(self, *, baseline_error=None, prepare_error=None, commit_error=None):
        self.baseline_error = baseline_error
        self.prepare_error = prepare_error
        self.commit_error = commit_error
        self.baseline_calls = []
        self.prepare_calls = []
        self.commit_calls = []
        self.no_commit_calls = []
        self.partial_paths = ("calculator.py",)

    def capture_baseline(self, task_id):
        self.baseline_calls.append(task_id)
        if self.baseline_error:
            raise self.baseline_error
        return GitBaseline(task_id, "/repo", "a" * 40, ())

    def capture_partial_paths(self, baseline):
        return self.partial_paths

    def prepare_change_set(self, baseline, report, owned_paths):
        self.prepare_calls.append((baseline, report, owned_paths))
        if self.prepare_error:
            raise self.prepare_error
        return GitChangeSet(
            baseline.task_id, baseline.repository_root, baseline.baseline_head,
            tuple(sorted(owned_paths)), (), (),
        )

    def commit(self, change_set, task):
        self.commit_calls.append((change_set, task))
        if self.commit_error:
            raise self.commit_error
        return GitCommitResult(
            task.id, change_set.repository_root, change_set.baseline_head,
            "b" * 40, "feat(task): delivery", change_set.changed_paths,
            change_set.changed_paths, NOW,
        )

    def verify_no_commit(self, change_set, task):
        self.no_commit_calls.append((change_set, task))
        return GitNoCommitResult(
            task.id,
            change_set.repository_root,
            change_set.baseline_head,
            change_set.baseline_head,
            NOW,
        )


class TaskCycleContractTests(unittest.TestCase):
    def test_config_and_request_validation(self):
        self.assertEqual(TaskCycleConfig(1).max_attempts, 1)
        for value in (0, -1):
            with self.assertRaises(ValueError):
                TaskCycleConfig(value)
        with self.assertRaises(ValueError):
            TaskCycleRequest(cycle_state().tasks[0], "")

    def test_start_preconditions_fail_closed_without_dependencies(self):
        base = cycle_state()
        states = (
            replace(base, tasks=()),
            replace(base, project=replace(base.project, current_task_id=None)),
            replace(
                base,
                tasks=(replace(base.tasks[0], status=TaskStatus.PENDING),),
            ),
            replace(
                base,
                project=replace(base.project, status=ProjectStatus.PAUSED_BY_BOSS),
            ),
        )
        for state in states:
            with self.subTest(state=state):
                store = FakeStore(state)
                session = FakeWorkerSession()
                supervisor = FakeSupervisor([])
                service, request, _, _, _, sessions = build_cycle(
                    store=store, session=session, supervisor=supervisor
                )
                with self.assertRaises(InvalidTaskCycleState):
                    service.execute(request)
                self.assertEqual(sessions, [])
                self.assertEqual(store.saved, [])


class TaskCycleGitDeliveryTests(unittest.TestCase):
    @staticmethod
    def capability_request(*, request_id="approval-1", thread_id="thread-1"):
        return WorkerCapabilityApprovalRequest(
            method="mcpServer/elicitation/request",
            protocol_request_id=request_id,
            thread_id=thread_id,
            turn_id="turn-1",
            server_name="cua_repl",
            request="Allow Computer Use to control Google Chrome?",
            capability="Computer Use",
            application="Google Chrome",
            capability_id="browser-use",
            tool_name="control_browser",
            available_scopes=(CapabilityApprovalScope.ONCE,),
        )

    def test_native_capability_request_persists_approval_not_worker_input(self):
        delivery = FakeGitDelivery()
        session = FakeWorkerSession(
            [CodexCapabilityApprovalRequired(self.capability_request())]
        )
        service, request, store, session, supervisor, _ = build_cycle(
            session=session,
            supervisor=FakeSupervisor([]),
            git_delivery=delivery,
        )

        outcome = service.execute(request)

        self.assertTrue(outcome.human_action_required)
        action = store.current.human_actions[-1]
        self.assertIs(action.category, HumanActionCategory.WORKER_APPROVAL)
        self.assertIsNone(action.worker_input)
        self.assertEqual(action.capability_approval.capability, "Computer Use")
        self.assertEqual(action.capability_approval.application, "Google Chrome")
        self.assertEqual(action.capability_approval.partial_paths, ("calculator.py",))
        self.assertEqual(action.capability_approval.baseline_head, "a" * 40)
        self.assertFalse(action.capability_approval.native_request_active)
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(delivery.commit_calls, [])
        self.assertEqual(session.closed, 1)

    def test_repeated_structured_permission_fails_closed_without_unbounded_actions(self):
        delivery = FakeGitDelivery()
        store = FakeStore(cycle_state())
        first = FakeWorkerSession(
            [CodexCapabilityApprovalRequired(self.capability_request())]
        )
        service, request, store, _, _, _ = build_cycle(
            store=store,
            session=first,
            supervisor=FakeSupervisor([]),
            git_delivery=delivery,
        )
        service.execute(request)
        first_action = store.current.human_actions[-1]
        closed = replace(
            first_action,
            status=HumanActionStatus.APPROVED,
            resolved_at=NOW,
        )
        task = replace(store.current.tasks[0], status=TaskStatus.IN_PROGRESS)
        store.save(
            replace(
                store.current,
                project=replace(
                    store.current.project,
                    status=ProjectStatus.RUNNING,
                    current_task_id=task.id,
                ),
                tasks=(task,),
                human_actions=(closed,),
            )
        )
        second = FakeWorkerSession(
            [
                CodexCapabilityApprovalRequired(
                    self.capability_request(
                        request_id="approval-2", thread_id="thread-2"
                    )
                )
            ]
        )
        second_service, second_request, _, _, _, _ = build_cycle(
            store=store,
            session=second,
            supervisor=FakeSupervisor([]),
            git_delivery=delivery,
        )

        outcome = second_service.execute(second_request)

        self.assertTrue(outcome.human_action_required)
        self.assertEqual(len(store.current.human_actions), 2)
        repeated = store.current.human_actions[-1]
        self.assertIs(repeated.category, HumanActionCategory.RECOVERY_UNCERTAIN)
        self.assertIn("Permission continuation failed", repeated.summary)
        self.assertEqual(
            repeated.capability_approval.identity,
            first_action.capability_approval.identity,
        )
        self.assertEqual(delivery.commit_calls, [])

    def test_answer_reuses_original_baseline_for_one_fresh_worker(self):
        delivery = FakeGitDelivery()
        first_session = FakeWorkerSession(
            outcomes=[
                CodexUserInputRequired(
                    WorkerInputRequest(
                        "item/tool/requestUserInput",
                        "request-1",
                        "Which database?",
                        ("SQLite", "PostgreSQL"),
                    )
                )
            ]
        )
        store = FakeStore(cycle_state())
        first, request, _, _, _, first_sessions = build_cycle(
            store=store, session=first_session, git_delivery=delivery
        )
        outcome = first.execute(request)
        self.assertTrue(outcome.human_action_required)
        action = store.current.human_actions[-1]
        self.assertEqual(action.worker_input.baseline_head, "a" * 40)
        self.assertEqual(action.worker_input.partial_paths, ("calculator.py",))
        self.assertEqual(len(store.current.git_baselines), 1)
        self.assertEqual(delivery.commit_calls, [])

        HumanResolutionService(
            store,
            clock=lambda: NOW,
            event_id_factory=IdFactory("human-event"),
            resolution_id_factory=IdFactory("resolution"),
        ).answer(action.id, "SQLite")
        reopened = store.current.tasks[0]
        active = replace(reopened, status=TaskStatus.IN_PROGRESS)
        store.save(
            replace(
                store.current,
                project=replace(store.current.project, current_task_id=active.id),
                tasks=(active,),
            )
        )

        second_session = FakeWorkerSession()
        second, second_request, _, _, _, second_sessions = build_cycle(
            store=store, session=second_session, git_delivery=delivery
        )
        result = second.execute(second_request)
        self.assertFalse(result.human_action_required)
        self.assertEqual(len(first_sessions), 1)
        self.assertEqual(len(second_sessions), 1)
        self.assertIsNot(first_sessions[0], second_sessions[0])
        self.assertEqual(delivery.baseline_calls, [active.id])
        self.assertEqual(len(store.current.git_baselines), 1)
        continued_prompt = second_session.requests[0][0].prompt
        self.assertIn("Boss answer: SQLite", continued_prompt)
        self.assertIn("original clean Git baseline", continued_prompt)
        self.assertEqual(len(delivery.commit_calls), 1)

    def test_continuation_workspace_drift_blocks_before_fresh_worker(self):
        delivery = FakeGitDelivery()
        first_session = FakeWorkerSession(
            outcomes=[
                CodexUserInputRequired(
                    WorkerInputRequest(
                        "item/tool/requestUserInput", None, "Question?", ()
                    )
                )
            ]
        )
        store = FakeStore(cycle_state())
        first, request, _, _, _, _ = build_cycle(
            store=store, session=first_session, git_delivery=delivery
        )
        first.execute(request)
        action = store.current.human_actions[-1]
        HumanResolutionService(
            store,
            clock=lambda: NOW,
            event_id_factory=IdFactory("human-event"),
            resolution_id_factory=IdFactory("resolution"),
        ).answer(action.id, "Answer")
        active = replace(store.current.tasks[0], status=TaskStatus.IN_PROGRESS)
        store.save(
            replace(
                store.current,
                project=replace(store.current.project, current_task_id=active.id),
                tasks=(active,),
            )
        )
        delivery.partial_paths = ("external.py",)
        second_session = FakeWorkerSession()
        second, second_request, _, _, _, second_sessions = build_cycle(
            store=store, session=second_session, git_delivery=delivery
        )
        outcome = second.execute(second_request)
        self.assertTrue(outcome.human_action_required)
        self.assertEqual(second_sessions, [])
        self.assertIs(
            store.current.human_actions[-1].category,
            HumanActionCategory.RECOVERY_UNCERTAIN,
        )

    def test_accepted_task_commits_before_completion_and_persists_sha(self):
        delivery = FakeGitDelivery()
        service, request, store, _, _, _ = build_cycle(git_delivery=delivery)
        outcome = service.execute(request)
        self.assertFalse(outcome.human_action_required)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(store.current.git_commit_results[0].commit_sha, "b" * 40)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertLess(event_types.index("git.committed"), event_types.index("task.completed"))
        self.assertEqual(len(delivery.commit_calls), 1)

    def test_verified_no_change_task_completes_only_after_supervisor_review(self):
        class VerificationOnlyWorker(FakeWorkerSession):
            def execute(self, request, *, report_id, created_at):
                report = super().execute(
                    request, report_id=report_id, created_at=created_at
                )
                return replace(
                    report,
                    files_changed=(),
                    git_state="clean",
                    summary="verification completed without repository changes",
                )

        delivery = FakeGitDelivery()
        delivery.partial_paths = ()
        supervisor = FakeSupervisor(
            [review(SupervisorDecisionType.CONTINUE)]
        )
        service, request, store, _, _, _ = build_cycle(
            session=VerificationOnlyWorker(),
            supervisor=supervisor,
            git_delivery=delivery,
        )

        outcome = service.execute(request)

        self.assertFalse(outcome.human_action_required)
        self.assertEqual(len(supervisor.requests), 1)
        self.assertEqual(
            supervisor.requests[0].execution_report.files_changed, ()
        )
        self.assertEqual(delivery.commit_calls, [])
        self.assertEqual(len(delivery.no_commit_calls), 1)
        self.assertEqual(store.current.git_commit_results, ())
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertIsNone(store.current.project.current_task_id)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertLess(
            event_types.index("supervisor.review_completed"),
            event_types.index("git.no_commit_required"),
        )
        self.assertLess(
            event_types.index("git.no_commit_required"),
            event_types.index("task.completed"),
        )
        self.assertEqual(
            store.current.latest_safe_point.head_sha,
            "a" * 40,
        )

    def test_no_change_worker_human_action_never_reaches_review_or_delivery(self):
        delivery = FakeGitDelivery()
        delivery.partial_paths = ()
        session = FakeWorkerSession(human_action_required=True)
        supervisor = FakeSupervisor([])
        service, request, store, _, _, _ = build_cycle(
            session=session,
            supervisor=supervisor,
            git_delivery=delivery,
        )

        outcome = service.execute(request)

        self.assertTrue(outcome.human_action_required)
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(delivery.prepare_calls, [])
        self.assertEqual(delivery.no_commit_calls, [])
        self.assertEqual(delivery.commit_calls, [])
        self.assertIs(store.current.tasks[0].status, TaskStatus.IN_PROGRESS)

    def test_no_change_supervisor_human_decision_never_completes_delivery(self):
        class VerificationOnlyWorker(FakeWorkerSession):
            def execute(self, request, *, report_id, created_at):
                return replace(
                    super().execute(
                        request, report_id=report_id, created_at=created_at
                    ),
                    files_changed=(),
                    git_state="clean",
                )

        delivery = FakeGitDelivery()
        delivery.partial_paths = ()
        service, request, store, _, _, _ = build_cycle(
            session=VerificationOnlyWorker(),
            supervisor=FakeSupervisor(
                [review(SupervisorDecisionType.HUMAN_REQUIRED)]
            ),
            git_delivery=delivery,
        )

        outcome = service.execute(request)

        self.assertTrue(outcome.human_action_required)
        self.assertEqual(delivery.no_commit_calls, [])
        self.assertEqual(delivery.commit_calls, [])
        self.assertIs(store.current.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_rework_does_not_commit_until_final_accepted_attempt(self):
        delivery = FakeGitDelivery()
        supervisor = FakeSupervisor(
            [
                review(SupervisorDecisionType.REWORK, "fix it"),
                review(SupervisorDecisionType.CONTINUE),
            ]
        )
        session = FakeWorkerSession()
        service, request, store, _, _, _ = build_cycle(
            git_delivery=delivery, supervisor=supervisor, session=session
        )
        service.execute(request)
        self.assertEqual(len(delivery.prepare_calls), 2)
        self.assertEqual(len(delivery.commit_calls), 1)
        self.assertEqual(len(store.current.git_commit_results), 1)
        self.assertEqual(store.current.tasks[0].execution_attempts, 2)

    def test_dirty_baseline_stops_before_worker_dispatch(self):
        delivery = FakeGitDelivery(
            baseline_error=DirtyGitBaseline("dirty workspace")
        )
        service, request, store, session, supervisor, sessions = build_cycle(
            git_delivery=delivery
        )
        outcome = service.execute(request)
        self.assertTrue(outcome.human_action_required)
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(sessions, [])
        self.assertEqual(session.started, 0)
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(delivery.commit_calls, [])
        self.assertIs(
            store.current.human_actions[-1].category,
            HumanActionCategory.WORKSPACE_BLOCK,
        )
        self.assertIn(
            "workspace precondition", store.current.human_actions[-1].summary
        )
        self.assertEqual(store.current.tasks[0].execution_attempts, 0)

    def test_worker_human_gate_and_supervisor_failure_never_commit(self):
        delivery = FakeGitDelivery()
        session = FakeWorkerSession(human_action_required=True)
        service, request, _, _, _, _ = build_cycle(
            git_delivery=delivery, session=session
        )
        self.assertTrue(service.execute(request).human_action_required)
        self.assertEqual(delivery.commit_calls, [])

        delivery = FakeGitDelivery()
        supervisor = FakeSupervisor([RuntimeError("review unavailable")])
        service, request, store, _, _, _ = build_cycle(
            git_delivery=delivery, supervisor=supervisor
        )
        with self.assertRaises(RuntimeError):
            service.execute(request)
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(delivery.commit_calls, [])

    def test_commit_failure_enters_human_required_without_completion(self):
        delivery = FakeGitDelivery(commit_error=GitCommitError("commit failed"))
        service, request, store, _, _, _ = build_cycle(git_delivery=delivery)
        outcome = service.execute(request)
        self.assertTrue(outcome.human_action_required)
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertIs(store.current.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(store.current.git_commit_results, ())
        self.assertEqual(len(delivery.commit_calls), 1)
        self.assertIs(
            store.current.human_actions[-1].category,
            HumanActionCategory.RECOVERY_UNCERTAIN,
        )


class TaskCycleFlowTests(unittest.TestCase):
    def test_boss_change_during_supervisor_review_is_preserved_at_safe_point(self):
        store = FakeStore(replace(cycle_state(), change_requests=()))
        orchestrator = OrchestratorService(
            store,
            clock=lambda: NOW,
            event_id_factory=IdFactory("boss-event"),
        )
        observed_after_change = []

        class ChangeInjectingSupervisor(FakeSupervisor):
            def review(self, request):
                orchestrator.change(
                    ChangeCommand(
                        "project-1",
                        "Change during Task execution.",
                        "boss",
                        "change-during-review",
                    )
                )
                project = store.load().project
                observed_after_change.append(
                    (project.status, project.current_task_id)
                )
                return super().review(request)

        supervisor = ChangeInjectingSupervisor(
            [review(SupervisorDecisionType.CONTINUE)]
        )
        service, request, _, _, _, _ = build_cycle(
            store=store,
            supervisor=supervisor,
        )

        service.execute(request)

        self.assertEqual(
            observed_after_change,
            [(ProjectStatus.CHANGE_REQUESTED, request.task.id)],
        )
        self.assertIs(store.current.project.status, ProjectStatus.CHANGE_REQUESTED)
        self.assertIsNone(store.current.project.current_task_id)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(
            store.current.change_requests[-1].id,
            "change-during-review",
        )

        observed_replanning_state = []

        class ReplanningProbe:
            def analyze_change(self, request):
                observed_replanning_state.append(
                    (
                        request.project_state.project.status,
                        request.project_state.project.current_task_id,
                    )
                )
                raise RuntimeError("stop after observing replanning handoff")

        replanning = ChangeReplanningService(
            store=store,
            supervisor=ReplanningProbe(),
            clock=lambda: NOW,
            plan_id_factory=IdFactory("replacement-plan"),
            event_id_factory=IdFactory("replanning-event"),
        )
        with self.assertRaises(SupervisorReplanningError):
            replanning.replan(
                ChangeReplanningRequest(
                    "project-1",
                    "change-during-review",
                )
            )
        self.assertEqual(
            observed_replanning_state,
            [(ProjectStatus.REPLANNING, None)],
        )

    def test_boss_pause_during_supervisor_review_is_preserved_at_safe_point(self):
        store = FakeStore(cycle_state())
        orchestrator = OrchestratorService(
            store,
            clock=lambda: NOW,
            event_id_factory=IdFactory("boss-event"),
        )

        class PauseInjectingSupervisor(FakeSupervisor):
            def review(self, request):
                orchestrator.pause(PauseCommand("project-1"))
                return super().review(request)

        supervisor = PauseInjectingSupervisor(
            [review(SupervisorDecisionType.CONTINUE)]
        )
        service, request, _, _, _, _ = build_cycle(
            store=store,
            supervisor=supervisor,
        )

        service.execute(request)

        self.assertIs(store.current.project.status, ProjectStatus.PAUSED_BY_BOSS)
        self.assertIsNone(store.current.project.current_task_id)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)

    def test_boss_pause_then_human_review_persists_typed_gate(self):
        store = FakeStore(cycle_state())
        orchestrator = OrchestratorService(
            store,
            clock=lambda: NOW,
            event_id_factory=IdFactory("boss-event"),
        )

        class PauseInjectingSupervisor(FakeSupervisor):
            def review(self, request):
                orchestrator.pause(PauseCommand("project-1"))
                return super().review(request)

        supervisor = PauseInjectingSupervisor(
            [review(SupervisorDecisionType.HUMAN_REQUIRED)]
        )
        service, request, _, _, _, _ = build_cycle(
            store=store,
            supervisor=supervisor,
        )

        outcome = service.execute(request)

        self.assertTrue(outcome.human_action_required)
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(len(store.current.human_actions), 1)
        self.assertEqual(
            store.current.human_actions[0].summary,
            "Supervisor requested human judgment",
        )

    def test_change_persisted_during_worker_is_preserved_at_safe_point(self):
        store = FakeStore(cycle_state())

        class ChangeInjectingWorker(FakeWorkerSession):
            def execute(self, request, *, report_id, created_at):
                current = store.load()
                change = replace(
                    current.change_requests[0],
                    id="change-safe-point",
                    status=ChangeRequestStatus.PENDING,
                )
                store.current = replace(
                    current,
                    project=replace(
                        current.project,
                        status=ProjectStatus.CHANGE_REQUESTED,
                    ),
                    change_requests=current.change_requests + (change,),
                )
                return super().execute(
                    request, report_id=report_id, created_at=created_at
                )

        session = ChangeInjectingWorker()
        service, request, _, _, supervisor, _ = build_cycle(
            store=store, session=session
        )

        service.execute(request)

        self.assertIs(store.current.project.status, ProjectStatus.CHANGE_REQUESTED)
        self.assertIsNone(store.current.project.current_task_id)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(
            store.current.change_requests[-1].id, "change-safe-point"
        )
        self.assertIs(
            supervisor.requests[0].project_state.project.status,
            ProjectStatus.CHANGE_REQUESTED,
        )

    def test_rework_progress_exposes_attempts_and_supervisor_decisions(self):
        progress = RecordingProgressSink()
        supervisor = FakeSupervisor(
            [
                review(SupervisorDecisionType.REWORK, "repair addition"),
                review(SupervisorDecisionType.CONTINUE),
            ]
        )
        service, request, _, _, _, _ = build_cycle(
            supervisor=supervisor,
            progress_sink=progress,
        )
        service.execute(request)
        relevant = tuple(
            event
            for event in progress.events
            if event.type
            in {
                ProgressEventType.TASK_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
                ProgressEventType.TASK_REWORK,
                ProgressEventType.TASK_COMPLETED,
            }
        )
        self.assertEqual(
            tuple(event.type for event in relevant),
            (
                ProgressEventType.TASK_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
                ProgressEventType.TASK_REWORK,
                ProgressEventType.TASK_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_REVIEW_COMPLETED,
                ProgressEventType.TASK_COMPLETED,
            ),
        )
        self.assertEqual(
            tuple(
                event.attempt
                for event in relevant
                if event.type is ProgressEventType.TASK_STARTED
            ),
            (1, 2),
        )
        decisions = tuple(
            event.metadata["decision"]
            for event in relevant
            if event.type is ProgressEventType.SUPERVISOR_REVIEW_COMPLETED
        )
        self.assertEqual(decisions, ("rework", "continue"))

    def test_continue_runs_one_turn_persists_and_completes_only_current_task(self):
        original = cycle_state()
        store = FakeStore(original)
        service, request, store, session, supervisor, sessions = build_cycle(store=store)

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.CONTINUE)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(len(outcome.execution_reports), 1)
        self.assertEqual(len(outcome.decisions), 1)
        self.assertFalse(outcome.human_action_required)
        self.assertEqual(session.started, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(sessions, [session])
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(len(supervisor.requests), 1)
        reviewed_state = supervisor.requests[0].project_state
        self.assertEqual(reviewed_state.execution_reports[-1].id, "report-1")
        self.assertEqual(reviewed_state.tasks[0].execution_attempts, 1)
        self.assertEqual(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(store.current.tasks[0].execution_attempts, 1)
        self.assertIsNone(store.current.project.current_task_id)
        self.assertIs(store.current.project.status, ProjectStatus.RUNNING)
        self.assertEqual(outcome.execution_reports[0].created_at, NOW)
        self.assertEqual(outcome.decisions[0].created_at, NOW)
        self.assertEqual(outcome.execution_reports[0].id, "report-1")
        self.assertEqual(outcome.decisions[0].id, "decision-1")
        new_events = store.current.events[len(original.events) :]
        self.assertEqual(
            tuple(event.id for event in new_events),
            ("event-1", "event-2", "event-3", "event-4"),
        )
        self.assertEqual(
            tuple(event.event_type for event in new_events),
            (
                "task.execution_started",
                "task.execution_completed",
                "supervisor.review_completed",
                "task.completed",
            ),
        )
        self.assertNotIn("initial prompt", repr(new_events))
        self.assertEqual(original.tasks[0].execution_attempts, 0)
        self.assertEqual(original.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(original.execution_reports, ())
        self.assertEqual(original.decisions, ())

    def test_rework_reuses_same_session_and_second_prompt_then_continues(self):
        supervisor = FakeSupervisor(
            [
                review(SupervisorDecisionType.REWORK, "repair addition"),
                review(SupervisorDecisionType.CONTINUE),
            ]
        )
        service, request, store, session, supervisor, sessions = build_cycle(
            supervisor=supervisor
        )

        outcome = service.execute(request)

        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(len(outcome.execution_reports), 2)
        self.assertEqual(len(outcome.decisions), 2)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(session.started, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(
            [item[0].prompt for item in session.requests],
            ["initial prompt", "repair addition"],
        )
        self.assertIs(session.requests[0][3], session.requests[1][3])
        self.assertEqual(
            [item.attempt for item in outcome.execution_reports], [1, 2]
        )
        self.assertEqual(store.current.tasks[0].execution_attempts, 2)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertIn("task.rework_requested", event_types)

    def test_rework_at_limit_returns_human_required_without_fourth_turn(self):
        supervisor = FakeSupervisor(
            [
                review(SupervisorDecisionType.REWORK, "attempt 2"),
                review(SupervisorDecisionType.REWORK, "attempt 3"),
                review(SupervisorDecisionType.REWORK, "boss must inspect"),
            ]
        )
        service, request, store, session, _, _ = build_cycle(
            supervisor=supervisor, max_attempts=3
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertTrue(outcome.human_action_required)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(outcome.final_prompt, "boss must inspect")
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(store.current.tasks[0].execution_attempts, 3)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertIn("task.cycle_limit_reached", event_types)
        self.assertIn("task.human_required", event_types)

    def test_supervisor_human_required_stops_worker(self):
        supervisor = FakeSupervisor(
            [review(SupervisorDecisionType.HUMAN_REQUIRED)]
        )
        service, request, store, session, _, _ = build_cycle(supervisor=supervisor)

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(outcome.attempts, 1)
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_done_completes_task_but_does_not_complete_project(self):
        supervisor = FakeSupervisor([review(SupervisorDecisionType.DONE)])
        service, request, store, _, _, _ = build_cycle(supervisor=supervisor)
        outcome = service.execute(request)
        self.assertIs(outcome.final_decision, SupervisorDecisionType.DONE)
        self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
        self.assertIs(store.current.project.status, ProjectStatus.RUNNING)
        self.assertIsNone(store.current.project.current_task_id)


class TaskCycleFailureTests(unittest.TestCase):
    def test_worker_turn_timeout_requires_recovery_without_second_worker(self):
        session = FakeWorkerSession(
            [
                CodexTurnHardTimeout(
                    "deadline exceeded with private payload",
                    inactivity_timeout_seconds=120,
                    max_turn_seconds=900,
                )
            ]
        )
        progress = RecordingProgressSink()
        service, request, store, _, supervisor, sessions = build_cycle(
            session=session,
            progress_sink=progress,
        )

        outcome = service.execute(request)

        self.assertTrue(outcome.human_action_required)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(session.started, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(supervisor.requests, [])
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(
            store.current.human_actions[-1].category.value,
            "recovery_uncertain",
        )
        event_types = tuple(event.type for event in progress.events)
        self.assertIn(ProgressEventType.WORKER_FAILED, event_types)
        self.assertEqual(event_types[-1], ProgressEventType.HUMAN_GATE)
        failure_events = tuple(
            event
            for event in store.current.events
            if event.event_type == "task.execution_failed"
        )
        self.assertEqual(failure_events[-1].metadata["timeout_kind"], "hard")
        self.assertEqual(
            failure_events[-1].metadata["inactivity_timeout_seconds"], "120"
        )
        self.assertEqual(failure_events[-1].metadata["max_turn_seconds"], "900")
        self.assertNotIn("private payload", str(failure_events[-1].metadata))

    def test_worker_failure_persists_bounded_partial_path_diagnostics(self):
        session = FakeWorkerSession([CodexTurnHardTimeout("timeout")])
        git_delivery = FakeGitDelivery()
        service, request, store, _, _, _ = build_cycle(
            session=session, git_delivery=git_delivery
        )

        service.execute(request)

        event = next(
            event
            for event in reversed(store.current.events)
            if event.event_type == "task.execution_failed"
        )
        self.assertEqual(event.metadata["partial_path_count"], "1")
        self.assertEqual(event.metadata["partial_path_1"], "calculator.py")
        self.assertNotIn("raw_payload", event.metadata)

    def test_worker_failure_preserves_persisted_recovery_identity(self):
        base = cycle_state()
        lease = ExecutionLease(
            "lease-1",
            base.project.id,
            "owner-1",
            1234,
            NOW,
            NOW,
            ExecutionLeaseStatus.ACTIVE,
        )
        store = FakeStore(replace(base, execution_leases=(lease,)))

        def record_identity(task_id, thread_id, attempt):
            latest = store.load()
            active = latest.execution_leases[0]
            store.save(
                replace(
                    latest,
                    execution_leases=(
                        replace(
                            active,
                            current_task_id=task_id,
                            codex_thread_id=thread_id,
                            attempt=attempt,
                        ),
                    ),
                )
            )

        session = FakeWorkerSession([InvalidWorkerReport("malformed JSON")])
        service, request, _, _, _, _ = build_cycle(
            store=store,
            session=session,
            supervisor=FakeSupervisor([]),
            worker_identity_started=record_identity,
        )

        service.execute(request)

        persisted = store.current.execution_leases[0]
        self.assertEqual(
            (
                persisted.current_task_id,
                persisted.codex_thread_id,
                persisted.attempt,
            ),
            (request.task.id, "thread-1", 1),
        )

    def test_structured_worker_human_gate_is_persisted_without_supervisor(self):
        session = FakeWorkerSession(human_action_required=True)
        supervisor = FakeSupervisor([])
        service, request, store, _, supervisor, _ = build_cycle(
            session=session, supervisor=supervisor
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(len(store.current.execution_reports), 1)
        self.assertEqual(supervisor.requests, [])
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_report_category_uses_kind_even_when_prose_mentions_other_categories(self):
        for kind, category in (
            (WorkerHumanActionKind.INPUT, HumanActionCategory.WORKER_INPUT),
            (WorkerHumanActionKind.APPROVAL, HumanActionCategory.WORKER_APPROVAL),
            (WorkerHumanActionKind.EXTERNAL_SIDE_EFFECT, HumanActionCategory.EXTERNAL_SIDE_EFFECT),
        ):
            with self.subTest(kind=kind):
                action = WorkerHumanAction(
                    kind, "input approval external_side_effect", "Specific request"
                )

                class Session(FakeWorkerSession):
                    def execute(self, request, *, report_id, created_at):
                        return replace(
                            super().execute(request, report_id=report_id, created_at=created_at),
                            human_action=action,
                        )

                service, request, store, _, supervisor, _ = build_cycle(session=Session())
                service.execute(request)
                self.assertIs(store.current.human_actions[-1].category, category)
                self.assertEqual(store.current.human_actions[-1].requested_action, action.request)
                self.assertEqual(supervisor.requests, [])

    def test_worker_approval_returns_human_required_without_supervisor(self):
        session = FakeWorkerSession([CodexApprovalRequired("approval")])
        supervisor = FakeSupervisor([])
        progress = RecordingProgressSink()
        service, request, store, session, supervisor, _ = build_cycle(
            session=session,
            supervisor=supervisor,
            progress_sink=progress,
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(outcome.attempts, 0)
        self.assertEqual(supervisor.requests, [])
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertIn("task.execution_failed", event_types)
        self.assertIn("task.human_required", event_types)
        progress_types = tuple(event.type for event in progress.events)
        self.assertIn(ProgressEventType.WORKER_FAILED, progress_types)
        self.assertIn(ProgressEventType.TASK_HUMAN_REQUIRED, progress_types)
        self.assertIn(ProgressEventType.HUMAN_GATE, progress_types)

    def test_worker_input_preserves_actionable_request_without_raw_payload(self):
        request_details = WorkerInputRequest(
            "item/tool/requestUserInput",
            "request-7",
            "Which rendering mode should be used?",
            ("Canvas", "DOM"),
        )
        session = FakeWorkerSession([CodexUserInputRequired(request_details)])
        supervisor = FakeSupervisor([])
        service, request, store, _, _, _ = build_cycle(
            session=session,
            supervisor=supervisor,
        )

        outcome = service.execute(request)

        self.assertTrue(outcome.human_action_required)
        action = store.current.human_actions[-1]
        self.assertIs(action.category, HumanActionCategory.WORKER_INPUT)
        self.assertEqual(action.task_id, request.task.id)
        self.assertEqual(action.worker_input.question, request_details.question)
        self.assertEqual(action.worker_input.choices, request_details.choices)
        self.assertEqual(action.worker_input.request_method, request_details.method)
        self.assertEqual(action.worker_input.request_id, request_details.request_id)
        self.assertEqual(action.worker_input.worker_attempt, 1)
        self.assertEqual(supervisor.requests, [])
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertIn("task.execution_failed", event_types)
        self.assertIn("task.human_required", event_types)

    def test_malformed_worker_report_returns_human_without_prose_fallback(self):
        session = FakeWorkerSession([InvalidWorkerReport("malformed JSON")])
        supervisor = FakeSupervisor([])
        service, request, store, session, supervisor, _ = build_cycle(
            session=session, supervisor=supervisor
        )

        outcome = service.execute(request)

        self.assertIs(outcome.final_decision, SupervisorDecisionType.HUMAN_REQUIRED)
        self.assertEqual(outcome.execution_reports, ())
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(store.current.execution_reports, ())

    def test_report_save_failure_stops_before_supervisor_without_reexecution(self):
        store = FakeStore(cycle_state(), fail_on_save=5)
        supervisor = FakeSupervisor([review(SupervisorDecisionType.CONTINUE)])
        service, request, store, session, supervisor, _ = build_cycle(
            store=store, supervisor=supervisor
        )

        with self.assertRaisesRegex(OSError, "persistence failed"):
            service.execute(request)

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(store.current.execution_reports, ())
        self.assertEqual(session.closed, 1)

    def test_supervisor_failure_keeps_report_persisted_and_does_not_retry(self):
        supervisor = FakeSupervisor([RuntimeError("provider failed")])
        progress = RecordingProgressSink()
        service, request, store, session, _, _ = build_cycle(
            supervisor=supervisor,
            progress_sink=progress,
        )

        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            service.execute(request)

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(len(store.current.execution_reports), 1)
        self.assertEqual(store.current.tasks[0].execution_attempts, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(
            tuple(
                event.type
                for event in progress.events
                if event.type
                in {
                    ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                    ProgressEventType.SUPERVISOR_FAILED,
                }
            ),
            (
                ProgressEventType.SUPERVISOR_REVIEW_STARTED,
                ProgressEventType.SUPERVISOR_FAILED,
            ),
        )


if __name__ == "__main__":
    unittest.main()
