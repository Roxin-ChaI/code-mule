from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import tempfile
import unittest

from code_mule.domain import (
    CapabilityApprovalScope,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    ProjectStatus,
    ProjectRevision,
    TaskStatus,
    WorkerInputDetails,
    WorkerCapabilityApprovalDetails,
)
from code_mule.git_delivery import GitBaseline
from code_mule.git_delivery.recovery import no_change_delivery_recovery_evidence
from code_mule.human import (
    HumanActionNotFound,
    HumanResolutionService,
    InvalidHumanResolution,
    request_human_action,
    allowed_resolution_strategies,
)
from code_mule.recovery import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionPhase,
    RecoveryMode,
)
from code_mule.recovery.service import RecoveryClassifier
from planning.test_validation import empty_state
from state import make_project_state


NOW = datetime(2026, 9, 2, tzinfo=UTC)


class MemoryStore:
    def __init__(self, state): self.state = state
    def load(self): return self.state
    def save(self, state): self.state = state


def gated_state(category=HumanActionCategory.WORKER_APPROVAL):
    ids = iter(("source", "requested"))
    return request_human_action(
        make_project_state(),
        category=category,
        summary="Action required",
        requested_action="Choose explicitly",
        risk="Specific risk",
        task_id="task-1",
        operation_time=NOW,
        action_id="action-1",
        event_id_factory=lambda: next(ids),
        source_event_types=("task.human_required",),
    )


def worker_input_state():
    source = gated_state(HumanActionCategory.WORKER_INPUT)
    action = replace(
        source.human_actions[0],
        worker_input=WorkerInputDetails(
            request_method="item/tool/requestUserInput",
            request_id="request-1",
            question="Which database?",
            choices=("SQLite", "PostgreSQL"),
            worker_attempt=1,
            baseline_head="a" * 40,
            partial_paths=("storage.py",),
        ),
    )
    return replace(
        source,
        human_actions=(action,),
        git_baselines=(GitBaseline("task-1", "/repo", "a" * 40, ()),),
    )


def capability_approval_state():
    source = gated_state(HumanActionCategory.WORKER_APPROVAL)
    details = WorkerCapabilityApprovalDetails(
        request_method="mcpServer/elicitation/request",
        request_id="88",
        thread_id="thread-1",
        turn_id="turn-1",
        server_name="cua_repl",
        capability="Computer Use",
        application="Google Chrome",
        capability_id="browser-use",
        tool_name="control_browser",
        approval_scopes=(CapabilityApprovalScope.ONCE,),
        worker_attempt=1,
        baseline_head="a" * 40,
        partial_paths=("index.html",),
        native_request_active=False,
    )
    return replace(
        source,
        human_actions=(replace(source.human_actions[0], capability_approval=details),),
    )


def planning_failure_state():
    base = empty_state(status=ProjectStatus.PLANNING)
    base = replace(
        base,
        project=replace(base.project, objective="Build a calculator"),
    )
    identifiers = iter(("planning-source", "planning-requested"))
    return request_human_action(
        base,
        category=HumanActionCategory.SUPERVISOR_FAILURE,
        summary="Planning failed",
        requested_action="Inspect and resolve",
        risk="No Plan is trusted",
        task_id=None,
        operation_time=NOW,
        action_id="planning-action",
        event_id_factory=lambda: next(identifiers),
        source_event_types=("planning.failed",),
        source_metadata={
            "operation": "plan",
            "failure_category": "provider_authentication",
            "attempt_count": "1",
        },
        phase=ExecutionPhase.PLANNING,
    )


def service(store):
    return HumanResolutionService(
        store,
        clock=lambda: NOW,
        event_id_factory=lambda: "human-event",
        resolution_id_factory=lambda: "resolution-1",
    )


def no_change_gate(root: Path):
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(
        ("git", "config", "user.name", "Code Mule Test"), cwd=root, check=True
    )
    subprocess.run(
        ("git", "config", "user.email", "code-mule@example.invalid"),
        cwd=root,
        check=True,
    )
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(("git", "add", "--", "README.md"), cwd=root, check=True)
    subprocess.run(
        ("git", "commit", "-q", "-m", "initial"), cwd=root, check=True
    )
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    base = make_project_state()
    task = replace(base.tasks[0], execution_attempts=1)
    report = replace(
        base.execution_reports[0],
        attempt=1,
        status="completed",
        files_changed=(),
        tests=("Local tests: pass",),
        static_checks=("Git clean: pass",),
        git_state="clean",
        issues=(),
        human_action=None,
    )
    state = replace(
        base,
        project=replace(
            base.project,
            status=ProjectStatus.RUNNING,
            current_task_id=task.id,
            workspace=str(root),
        ),
        tasks=(task,),
        decisions=(),
        execution_reports=(report,),
        human_actions=(),
        human_resolutions=(),
        events=(),
        execution_leases=(),
        git_baselines=(GitBaseline(task.id, str(root), head, ()),),
        git_change_sets=(),
        git_commit_results=(),
        execution_attempts=(
            ExecutionAttempt(
                task.id,
                1,
                ExecutionAttemptStatus.REPORT_PERSISTED,
                NOW,
                thread_id="thread-1",
                turn_id="turn-1",
                baseline_head=head,
                terminal_at=NOW,
            ),
        ),
        latest_execution_stop=None,
        latest_safe_point=None,
    )
    identifiers = iter(("action-requested", "delivery-failed"))
    return request_human_action(
        state,
        category=HumanActionCategory.RECOVERY_UNCERTAIN,
        summary="Task Git delivery could not be completed safely",
        requested_action="Inspect and choose an explicit recovery action",
        risk="The Task delivery boundary must remain untrusted",
        task_id=task.id,
        operation_time=NOW,
        action_id="no-change-action",
        event_id_factory=lambda: next(identifiers),
        source_event_types=("git.delivery_failed",),
        source_metadata={
            "error_type": "EmptyGitChangeSet",
            "stage": "ownership",
        },
    )


class HumanResolutionServiceTests(unittest.TestCase):
    def test_legacy_empty_change_gate_can_continue_only_from_exact_report(self):
        with tempfile.TemporaryDirectory() as directory:
            state = no_change_gate(Path(directory))
            action = state.human_actions[-1]
            evidence = no_change_delivery_recovery_evidence(state, action)
            self.assertTrue(evidence.continuation_safe)
            self.assertFalse(evidence.supervisor_reviewed)
            self.assertFalse(evidence.commit_created)
            self.assertEqual(
                allowed_resolution_strategies(state, action),
                (
                    HumanResolutionStrategy.CONTINUE_AFTER_REPORT,
                    HumanResolutionStrategy.FAIL_PROJECT,
                    HumanResolutionStrategy.ACKNOWLEDGE,
                ),
            )

            store = MemoryStore(state)
            updated = service(store).resolve(
                action.id, HumanResolutionStrategy.CONTINUE_AFTER_REPORT
            )

            self.assertIs(updated.project.status, ProjectStatus.RUNNING)
            self.assertEqual(updated.project.current_task_id, action.task_id)
            self.assertIs(updated.human_actions[-1].status, HumanActionStatus.RESOLVED)
            self.assertIs(
                updated.execution_attempts[-1].status,
                ExecutionAttemptStatus.REPORT_PERSISTED,
            )
            self.assertEqual(updated.git_commit_results, ())
            self.assertFalse(
                any(
                    event.event_type == "task.execution_started"
                    for event in updated.events
                )
            )
            self.assertIs(
                RecoveryClassifier().classify(updated).recovery_mode,
                RecoveryMode.CONTINUE_AFTER_REPORT,
            )

    def test_legacy_empty_change_continuation_fails_closed_on_git_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = no_change_gate(root)
            (root / "unexpected.txt").write_text("drift\n", encoding="utf-8")
            action = state.human_actions[-1]

            evidence = no_change_delivery_recovery_evidence(state, action)

            self.assertFalse(evidence.continuation_safe)
            self.assertNotIn(
                HumanResolutionStrategy.CONTINUE_AFTER_REPORT,
                allowed_resolution_strategies(state, action),
            )
            store = MemoryStore(state)
            with self.assertRaisesRegex(InvalidHumanResolution, "not allowed"):
                service(store).resolve(
                    action.id, HumanResolutionStrategy.CONTINUE_AFTER_REPORT
                )
            self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_legacy_empty_change_proof_rejects_staging_head_and_report_drift(self):
        mutations = ("staged", "head", "report")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = no_change_gate(root)
                if mutation == "staged":
                    (root / "staged.txt").write_text("drift\n", encoding="utf-8")
                    subprocess.run(
                        ("git", "add", "--", "staged.txt"), cwd=root, check=True
                    )
                elif mutation == "head":
                    (root / "README.md").write_text("new head\n", encoding="utf-8")
                    subprocess.run(
                        ("git", "add", "--", "README.md"), cwd=root, check=True
                    )
                    subprocess.run(
                        ("git", "commit", "-q", "-m", "external"),
                        cwd=root,
                        check=True,
                    )
                else:
                    state = replace(
                        state,
                        execution_reports=(
                            replace(
                                state.execution_reports[0],
                                files_changed=("claimed.py",),
                                git_state="dirty",
                            ),
                        ),
                    )

                evidence = no_change_delivery_recovery_evidence(
                    state, state.human_actions[-1]
                )

                self.assertFalse(evidence.continuation_safe)
                self.assertNotIn(
                    HumanResolutionStrategy.CONTINUE_AFTER_REPORT,
                    allowed_resolution_strategies(state, state.human_actions[-1]),
                )
    def test_answer_reopens_task_without_starting_work_or_leaking_answer(self):
        store = MemoryStore(worker_input_state())
        updated = service(store).answer("action-1", "SQLite")
        action = updated.human_actions[0]
        self.assertIs(action.status, HumanActionStatus.RESOLVED)
        self.assertEqual(action.worker_input.answer, "SQLite")
        self.assertIs(updated.project.status, ProjectStatus.RUNNING)
        self.assertIsNone(updated.project.current_task_id)
        self.assertIs(updated.tasks[0].status, TaskStatus.REOPENED)
        self.assertIs(
            updated.human_resolutions[-1].strategy,
            HumanResolutionStrategy.ANSWER,
        )
        self.assertEqual(updated.events[-1].event_type, "human_action.answered")
        self.assertNotIn("SQLite", str(updated.events[-1].metadata))
        with self.assertRaises(InvalidHumanResolution):
            service(store).answer("action-1", "again")

    def test_answer_rejects_non_input_action_and_invalid_answer(self):
        store = MemoryStore(gated_state())
        with self.assertRaisesRegex(InvalidHumanResolution, "cannot accept"):
            service(store).answer("action-1", "yes")
        store = MemoryStore(worker_input_state())
        with self.assertRaisesRegex(InvalidHumanResolution, "empty"):
            service(store).answer("action-1", "")
    def test_approve_is_scoped_audited_and_cannot_be_reused(self):
        store = MemoryStore(gated_state())
        updated = service(store).approve("action-1")
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.APPROVED)
        self.assertIs(updated.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(updated.events[-1].event_type, "human_action.approved")
        self.assertIs(
            updated.human_resolutions[0].strategy,
            HumanResolutionStrategy.APPROVE,
        )
        with self.assertRaisesRegex(InvalidHumanResolution, "already closed"):
            service(store).approve("action-1")

    def test_expired_native_approval_decision_fails_project_without_retry(self):
        for operation, expected_status in (
            ("approve", HumanActionStatus.APPROVED),
            ("reject", HumanActionStatus.REJECTED),
        ):
            with self.subTest(operation=operation):
                store = MemoryStore(capability_approval_state())
                updated = getattr(service(store), operation)("action-1")
                self.assertIs(updated.project.status, ProjectStatus.FAILED)
                self.assertIsNone(updated.project.current_task_id)
                self.assertIs(updated.tasks[0].status, TaskStatus.BLOCKED)
                self.assertIs(updated.human_actions[0].status, expected_status)
                self.assertIn(
                    "worker.capability_approval_expired",
                    tuple(event.event_type for event in updated.events),
                )
                self.assertFalse(
                    any(event.event_type == "task.execution_started" for event in updated.events)
                )

    def test_unknown_action_and_wrong_operation_fail_closed(self):
        store = MemoryStore(gated_state(HumanActionCategory.ATTEMPT_LIMIT))
        with self.assertRaises(HumanActionNotFound):
            service(store).approve("future-action")
        with self.assertRaisesRegex(InvalidHumanResolution, "resolve"):
            service(store).approve("action-1")
        self.assertIs(store.state.human_actions[0].status, HumanActionStatus.PENDING)

    def test_reject_records_terminal_action_without_executing(self):
        store = MemoryStore(gated_state())
        updated = service(store).reject("action-1")
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.REJECTED)
        self.assertIs(updated.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(updated.events[-1].event_type, "human_action.rejected")
        self.assertIs(
            updated.human_resolutions[0].strategy,
            HumanResolutionStrategy.REJECT,
        )

    def test_attempt_limit_uses_explicit_resolve_and_reopens_task(self):
        store = MemoryStore(gated_state(HumanActionCategory.ATTEMPT_LIMIT))
        updated = service(store).resolve(
            "action-1", HumanResolutionStrategy.RETRY_TASK
        )
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.RESOLVED)
        self.assertIs(updated.project.status, ProjectStatus.RUNNING)
        self.assertIsNone(updated.project.current_task_id)
        self.assertIs(updated.tasks[0].status, TaskStatus.REOPENED)
        self.assertEqual(updated.human_resolutions[0].action_id, "action-1")
        self.assertEqual(updated.events[-1].event_type, "human_action.resolved")

    def test_workspace_block_can_retry_without_weakening_uncertain_recovery(self):
        store = MemoryStore(gated_state(HumanActionCategory.WORKSPACE_BLOCK))
        updated = service(store).resolve(
            "action-1", HumanResolutionStrategy.RETRY_TASK
        )
        self.assertIs(updated.project.status, ProjectStatus.RUNNING)
        self.assertIsNone(updated.project.current_task_id)
        self.assertIs(updated.tasks[0].status, TaskStatus.REOPENED)
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.RESOLVED)

    def test_uncertain_recovery_cannot_be_converted_to_retry(self):
        store = MemoryStore(gated_state(HumanActionCategory.RECOVERY_UNCERTAIN))
        with self.assertRaisesRegex(InvalidHumanResolution, "not safe"):
            service(store).resolve("action-1", HumanResolutionStrategy.RETRY_TASK)
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_uncertain_acknowledge_records_awareness_without_clearing_gate(self):
        store = MemoryStore(gated_state(HumanActionCategory.RECOVERY_UNCERTAIN))
        updated = service(store).resolve(
            "action-1", HumanResolutionStrategy.ACKNOWLEDGE
        )

        self.assertIs(updated.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertIs(updated.project.current_task_id, store.state.project.current_task_id)
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.PENDING)
        self.assertIs(updated.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(updated.events[-1].event_type, "human_action.acknowledged")
        self.assertEqual(
            allowed_resolution_strategies(updated, updated.human_actions[0]),
            (HumanResolutionStrategy.FAIL_PROJECT,),
        )
        self.assertFalse(
            any(event.event_type == "task.execution_started" for event in updated.events)
        )

    def test_fail_project_is_an_explicit_nonapproval_resolution(self):
        store = MemoryStore(gated_state(HumanActionCategory.SUPERVISOR_FAILURE))
        updated = service(store).resolve(
            "action-1", HumanResolutionStrategy.FAIL_PROJECT
        )
        self.assertIs(updated.project.status, ProjectStatus.FAILED)
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.RESOLVED)

    def test_planning_failure_exposes_and_applies_only_safe_fresh_retry(self):
        state = planning_failure_state()
        action = state.human_actions[0]
        self.assertEqual(
            allowed_resolution_strategies(state, action),
            (
                HumanResolutionStrategy.RETRY_PLANNING,
                HumanResolutionStrategy.FAIL_PROJECT,
                HumanResolutionStrategy.ACKNOWLEDGE,
            ),
        )
        store = MemoryStore(state)

        updated = service(store).resolve(
            action.id, HumanResolutionStrategy.RETRY_PLANNING
        )

        self.assertIs(updated.project.status, ProjectStatus.PLANNING)
        self.assertEqual(updated.plans, ())
        self.assertEqual(updated.revisions, ())
        self.assertEqual(updated.tasks, ())
        self.assertIs(
            RecoveryClassifier().classify(updated).recovery_mode,
            RecoveryMode.FRESH_PLANNING,
        )
        with self.assertRaisesRegex(InvalidHumanResolution, "not HUMAN_REQUIRED"):
            service(store).resolve(
                action.id, HumanResolutionStrategy.RETRY_PLANNING
            )

    def test_planning_failure_rejects_task_retry_deterministically(self):
        store = MemoryStore(planning_failure_state())
        with self.assertRaisesRegex(InvalidHumanResolution, "not allowed"):
            service(store).resolve(
                "planning-action", HumanResolutionStrategy.RETRY_TASK
            )
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_legacy_placeholder_revision_keeps_planning_retry_fail_closed(self):
        state = planning_failure_state()
        state = replace(
            state,
            revisions=(ProjectRevision(1, NOW),),
        )
        action = state.human_actions[0]

        self.assertEqual(
            allowed_resolution_strategies(state, action),
            (
                HumanResolutionStrategy.FAIL_PROJECT,
                HumanResolutionStrategy.ACKNOWLEDGE,
            ),
        )
        store = MemoryStore(state)
        with self.assertRaisesRegex(InvalidHumanResolution, "not safe"):
            service(store).resolve(
                action.id, HumanResolutionStrategy.RETRY_PLANNING
            )
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)


if __name__ == "__main__":
    unittest.main()
