import copy
import io
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli.contracts import CliExitCode
from code_mule.domain.enums import (
    ChangeRequestStatus,
    PlanStatus,
    ProjectStatus,
    RevisionCheckStatus,
    RevisionStatus,
    TaskStatus,
)
from code_mule.domain.models import (
    ChangeRequest,
    Milestone,
    Plan,
    Project,
    ProjectEvent,
    ProjectRevision,
    Requirement,
    Task,
)
from code_mule.domain.state_machine import (
    InvalidProjectTransition,
    validate_transition,
)
from code_mule.orchestrator import ChangeCommand, OrchestratorService
from code_mule.orchestrator.service import InvalidBossCommand
from code_mule.git_delivery import GitCommitResult
from code_mule.presentation import render_project
from code_mule.presentation.models import project_view
from code_mule.presentation.terminal import TerminalDashboard
from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCheck,
    ProjectVerificationResult,
    ProjectVerificationStatus,
)
from code_mule.project_verification.service import (
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.recovery import SafePoint, SafePointKind
from code_mule.replanning import ChangeReplanningRequest
from code_mule.replanning.errors import (
    InvalidReplanningState,
    ReplanMaterializationError,
)
from code_mule.replanning.reopen import PostCompletionReplanner
from code_mule.revision import latest_revision
from code_mule.state.models import ProjectState
from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    deserialize_project_state,
    serialize_project_state,
)
from code_mule.supervisor import (
    FinalReviewResult,
    ImpactAnalysisResult,
    MilestoneProposal,
    TaskProposal,
)


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
HEAD1 = "a" * 40


class MemoryStore:
    def __init__(self, state):
        self.state = state
        self.saved = []

    def load(self):
        return self.state

    def save(self, state):
        self.state = state
        self.saved.append(state)


class Clock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        value = NOW + timedelta(seconds=self.calls)
        self.calls += 1
        return value


class EventIds:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"event-{self.value}"


def task(task_id: str, milestone_id: str = "M1", *, completed: bool = True) -> Task:
    return Task(
        task_id,
        milestone_id,
        f"Implement {task_id}",
        f"Work for {task_id}",
        TaskStatus.COMPLETED if completed else TaskStatus.PENDING,
        (),
        ("verified",),
        1 if completed else 0,
        NOW,
        NOW,
        ("REQ-1",),
    )


def done_v1_state(*, verification=None) -> ProjectState:
    revision = ProjectRevision(
        revision_number=1,
        started_at=NOW,
        lifecycle_status=RevisionStatus.COMPLETED,
        plan_id="PLAN-1",
        plan_version=1,
        completed_at=NOW + timedelta(hours=1),
        completion_head=HEAD1,
        verification_status=RevisionCheckStatus.PASS,
        final_review_status=RevisionCheckStatus.PASS,
        verification_result_id=None if verification is None else verification.id,
    )
    return ProjectState(
        project=Project(
            "project-1",
            "Leaderboard",
            ProjectStatus.DONE,
            "PLAN-1",
            None,
            NOW,
            NOW + timedelta(hours=1),
        ),
        requirements=(
            Requirement(
                "REQ-1",
                "project-1",
                "Leaderboard",
                "Score leaderboard.",
                __import__("code_mule.domain", fromlist=["RequirementStatus"]).RequirementStatus.ACTIVE,
                "high",
                ("shows scores",),
                "boss",
                NOW,
                NOW,
            ),
        ),
        plans=(
            Plan(
                "PLAN-1",
                "project-1",
                1,
                PlanStatus.COMPLETED,
                ("REQ-1",),
                ("M1",),
                NOW,
            ),
        ),
        milestones=(Milestone("M1", "PLAN-1", "Leaderboard", "completed", ("T1", "T2", "T3")),),
        tasks=(
            task("T1"),
            task("T2"),
            task("T3"),
        ),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
        latest_safe_point=SafePoint(
            SafePointKind.PROJECT_DONE,
            NOW + timedelta(hours=1),
            head_sha=HEAD1,
        ),
        revisions=(revision,),
        project_verification_results=() if verification is None else (verification,),
    )


def verification_evidence() -> ProjectVerificationResult:
    checks = (
        ProjectVerificationCheck(
            "Git clean",
            ProjectVerificationCategory.GIT_CLEAN,
            ("git", "status"),
            ProjectVerificationStatus.PASS,
            0,
            "Clean at completion head.",
            True,
        ),
    )
    return ProjectVerificationResult(
        "verify-v1",
        "project-1",
        "PLAN-1",
        HEAD1,
        HEAD1,
        checks,
        NOW,
        NOW + timedelta(hours=1),
        FinalReviewDecision.APPROVE,
        "Approved.",
    )


def proposal_v2(task_id: str = "T4", supersedes: str = "T3") -> ImpactAnalysisResult:
    return ImpactAnalysisResult(
        change_request_id="CR-2",
        summary="Persist leaderboard across refreshes.",
        architecture_impact="Add local persistence.",
        affected_components=("leaderboard",),
        affected_requirement_ids=(),
        affected_task_ids=(supersedes,),
        affected_completed_tasks=(supersedes,),
        affected_in_progress_tasks=(),
        affected_pending_tasks=(),
        requirements_to_add=(),
        requirements_to_update=(),
        tasks_to_add=(
            TaskProposal(
                task_id,
                "Persist leaderboard",
                "Make the board survive refresh.",
                (),
                ("persistence verified",),
                ("REQ-1",),
                supersedes_task_id=supersedes,
            ),
        ),
        tasks_to_reopen=(),
        tasks_to_cancel=(),
        milestone_ids_reused=(),
        milestones=(MilestoneProposal("M2", "Persistence", (task_id,)),),
        dependency_changes=(),
        task_requirement_updates=(),
        risks=(),
        recommendation="Create Plan v2.",
        rationale="Persistence is new work on top of the completed leaderboard.",
    )


class FakeSupervisor:
    def __init__(self, proposal=None):
        self.proposal = proposal or proposal_v2()
        self.requests = []

    def analyze_change(self, request):
        self.requests.append(request)
        return self.proposal


def planner(store, proposal=None):
    return PostCompletionReplanner(
        store=store,
        supervisor=FakeSupervisor(proposal),
        clock=Clock(),
        plan_id_factory=lambda: f"PLAN-{len(store.load().plans) + 1}",
        event_id_factory=EventIds(),
    )


class PostCompletionRevisionTests(unittest.TestCase):
    def request_change(self, state):
        store = MemoryStore(state)
        OrchestratorService(
            store,
            clock=Clock(),
            event_id_factory=EventIds(),
        ).change(ChangeCommand("project-1", "Persist scores", "boss", "CR-2"))
        return store.state

    def test_done_to_change_requested_and_run_still_blocked(self):
        requested = self.request_change(done_v1_state())
        self.assertIs(
            requested.project.status,
            ProjectStatus.CHANGE_REQUESTED,
        )
        change = requested.change_requests[0]
        self.assertEqual(change.requested_revision, 2)
        self.assertEqual(change.base_revision, 1)
        self.assertEqual(change.base_plan_id, "PLAN-1")
        with self.assertRaises(InvalidProjectTransition):
            validate_transition(ProjectStatus.DONE, ProjectStatus.RUNNING)

    def test_done_change_cannot_be_issued_while_change_pending(self):
        requested = self.request_change(done_v1_state())
        store = MemoryStore(requested)
        with self.assertRaises(InvalidBossCommand):
            OrchestratorService(
                store,
                clock=Clock(),
                event_id_factory=EventIds(),
            ).change(ChangeCommand("project-1", "Second", "boss", "CR-3"))

    def test_running_and_paused_change_behavior_is_unchanged(self):
        for status in (ProjectStatus.RUNNING, ProjectStatus.PAUSED_BY_BOSS):
            with self.subTest(status=status):
                base = done_v1_state()
                state = replace(
                    base,
                    project=replace(
                        base.project,
                        status=status,
                    ),
                )
                # done fixture already has completed revision; make it in-progress.
                state = replace(
                    state,
                    revisions=(
                        replace(
                            state.revisions[0],
                            lifecycle_status=RevisionStatus.IN_PROGRESS,
                        ),
                    ),
                )
                store = MemoryStore(state)
                result = OrchestratorService(
                    store,
                    clock=Clock(),
                    event_id_factory=EventIds(),
                ).change(
                    ChangeCommand("project-1", "Mid-flight change", "boss", "CR-X")
                )
                self.assertEqual(result.current_status, ProjectStatus.CHANGE_REQUESTED)
                self.assertEqual(
                    store.state.change_requests[0].requested_revision,
                    1,
                )

    def test_replan_v2_preserves_history_lineage_and_resets_checks(self):
        requested = self.request_change(done_v1_state(verification=verification_evidence()))
        store = MemoryStore(requested)
        service = planner(store)
        outcome = service.replan(ChangeReplanningRequest("project-1", "CR-2"))

        state = store.state
        self.assertEqual(outcome.plan_version, 2)
        self.assertIs(state.project.status, ProjectStatus.RUNNING)
        self.assertEqual(len(state.plans), 2)
        self.assertEqual(state.plans[0].version, 1)
        self.assertEqual(state.plans[0].status, PlanStatus.COMPLETED)
        self.assertEqual(len(state.tasks), 4)
        self.assertEqual(
            state.tasks[0].status,
            TaskStatus.COMPLETED,
        )
        self.assertEqual(state.tasks[-1].supersedes_task_id, "T3")
        self.assertEqual(state.change_requests[0].status, ChangeRequestStatus.APPLIED)
        revisions = state.revisions
        self.assertEqual([item.revision_number for item in revisions], [1, 2])
        self.assertEqual(revisions[0].verification_status, RevisionCheckStatus.PASS)
        self.assertEqual(revisions[1].verification_status, RevisionCheckStatus.NOT_RUN)
        self.assertEqual(revisions[1].final_review_status, RevisionCheckStatus.NOT_RUN)

    def test_progress_counts_only_executable_revision_work(self):
        requested = self.request_change(done_v1_state())
        store = MemoryStore(requested)
        planner(store).replan(ChangeReplanningRequest("project-1", "CR-2"))
        state = store.state
        view = project_view(state)
        self.assertEqual(view.total_tasks, 1)
        self.assertEqual(view.completed_tasks, 0)
        self.assertEqual(view.revision_number, 2)
        self.assertEqual(view.plan_version, 2)
        rendered = "\n".join(
            render_project(
                state,
                terminal=TerminalDashboard.for_stream(io.StringIO()),
            )
        )
        self.assertIn("Revision    2", rendered)

    def test_duplicate_apply_and_applied_request_cannot_create_second_plan(self):
        requested = self.request_change(done_v1_state())
        store = MemoryStore(requested)
        planner(store).replan(ChangeReplanningRequest("project-1", "CR-2"))
        second = MemoryStore(store.state)
        with self.assertRaises(InvalidReplanningState):
            planner(second).replan(ChangeReplanningRequest("project-1", "CR-2"))
        self.assertEqual(len(second.state.plans), 2)
        self.assertEqual(len(second.saved), 0)

    def test_no_op_or_empty_proposal_is_rejected(self):
        requested = self.request_change(done_v1_state())
        empty = replace(
            proposal_v2(),
            tasks_to_add=(),
            milestones=(),
        )
        store = MemoryStore(requested)
        with self.assertRaises(ReplanMaterializationError):
            planner(store, empty).replan(
                ChangeReplanningRequest("project-1", "CR-2")
            )

    def test_restart_after_replanning_start_does_not_duplicate_revision(self):
        requested = self.request_change(done_v1_state())
        change = requested.change_requests[0]
        restarted = replace(
            requested,
            project=replace(
                requested.project,
                status=ProjectStatus.REPLANNING,
            ),
            change_requests=(
                replace(change, status=ChangeRequestStatus.ANALYZING),
            ),
        )
        store = MemoryStore(restarted)
        outcome = planner(store).replan(
            ChangeReplanningRequest("project-1", "CR-2")
        )
        self.assertEqual(outcome.plan_version, 2)
        self.assertEqual(
            [item.revision_number for item in store.state.revisions],
            [1, 2],
        )

    def test_revision_3_is_supported(self):
        requested = self.request_change(done_v1_state())
        store = MemoryStore(requested)
        planner(store).replan(ChangeReplanningRequest("project-1", "CR-2"))
        state = store.state
        v2_plan = state.plans[-1]
        v2_task = state.tasks[-1]
        completed = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.DONE,
            ),
            plans=tuple(
                replace(item, status=PlanStatus.COMPLETED)
                if item.id == v2_plan.id
                else item
                for item in state.plans
            ),
            tasks=tuple(
                replace(item, status=TaskStatus.COMPLETED)
                if item.id == v2_task.id
                else item
                for item in state.tasks
            ),
            revisions=(
                state.revisions[0],
                replace(
                    state.revisions[1],
                    lifecycle_status=RevisionStatus.COMPLETED,
                    plan_id=v2_plan.id,
                    plan_version=2,
                    completed_at=NOW + timedelta(hours=2),
                    verification_status=RevisionCheckStatus.PASS,
                    final_review_status=RevisionCheckStatus.PASS,
                ),
            ),
        )
        store2 = MemoryStore(completed)
        OrchestratorService(
            store2,
            clock=Clock(),
            event_id_factory=EventIds(),
        ).change(ChangeCommand("project-1", "Add difficulty", "boss", "CR-3"))
        v3 = replace(
            proposal_v2(task_id="T5", supersedes="T4"),
            change_request_id="CR-3",
            milestones=(MilestoneProposal("M3", "Difficulty", ("T5",)),),
        )
        outcome = planner(store2, v3).replan(
            ChangeReplanningRequest("project-1", "CR-3")
        )
        self.assertEqual(outcome.plan_version, 3)
        self.assertEqual(
            [item.revision_number for item in store2.state.revisions],
            [1, 2, 3],
        )
        self.assertEqual(store2.state.tasks[-1].supersedes_task_id, "T4")

    def test_git_drift_and_dirty_workspace_block_reopen(self):
        for change in ("drift", "dirty"):
            with self.subTest(change=change):
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
                    subprocess.run(
                        ("git", "config", "user.name", "T"),
                        cwd=root,
                        check=True,
                    )
                    subprocess.run(
                        ("git", "config", "user.email", "t@example.invalid"),
                        cwd=root,
                        check=True,
                    )
                    (root / "README.md").write_text("# Leaderboard\n")
                    subprocess.run(("git", "add", "README.md"), cwd=root, check=True)
                    subprocess.run(
                        ("git", "commit", "-qm", "baseline"),
                        cwd=root,
                        check=True,
                    )
                    head = subprocess.run(
                        ("git", "rev-parse", "HEAD"),
                        cwd=root,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip()
                    revision = ProjectRevision(
                        revision_number=1,
                        started_at=NOW,
                        lifecycle_status=RevisionStatus.COMPLETED,
                        plan_id="PLAN-1",
                        plan_version=1,
                        completed_at=NOW,
                        completion_head=head,
                        verification_status=RevisionCheckStatus.PASS,
                        final_review_status=RevisionCheckStatus.PASS,
                    )
                    state = replace(
                        done_v1_state(),
                        project=replace(
                            done_v1_state().project,
                            workspace=str(root),
                        ),
                        revisions=(revision,),
                    )
                    if change == "drift":
                        (root / "README.md").write_text("# Changed\n")
                        subprocess.run(("git", "add", "README.md"), cwd=root, check=True)
                        subprocess.run(
                            ("git", "commit", "-qm", "external"),
                            cwd=root,
                            check=True,
                        )
                    else:
                        (root / "dirty.txt").write_text("dirty\n")
                    requested = self.request_change(state)
                    store = MemoryStore(requested)
                    with self.assertRaises(InvalidReplanningState):
                        planner(store).replan(
                            ChangeReplanningRequest("project-1", "CR-2")
                        )

    def test_revision_two_completion_creates_new_immutable_record(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            subprocess.run(
                ("git", "config", "user.name", "T"),
                cwd=root,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.email", "t@example.invalid"),
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("# Leaderboard\n")
            subprocess.run(("git", "add", "README.md"), cwd=root, check=True)
            subprocess.run(
                ("git", "commit", "-qm", "revision 1"),
                cwd=root,
                check=True,
            )
            baseline = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            old_evidence = verification_evidence()
            state = replace(
                done_v1_state(verification=old_evidence),
                project=replace(
                    done_v1_state().project,
                    workspace=str(root),
                ),
                revisions=(
                    replace(
                        done_v1_state().revisions[0],
                        completion_head=baseline,
                    ),
                ),
            )
            requested = self.request_change(state)
            store = MemoryStore(requested)
            planner(store).replan(
                ChangeReplanningRequest("project-1", "CR-2")
            )
            reopened = store.state
            task_id = reopened.tasks[-1].id
            (root / "persistence.py").write_text("STORAGE = 'local'\n")
            subprocess.run(("git", "add", "persistence.py"), cwd=root, check=True)
            subprocess.run(
                ("git", "commit", "-qm", "feat(revision2): persist"),
                cwd=root,
                check=True,
            )
            head2 = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            commit = GitCommitResult(
                task_id,
                str(root),
                baseline,
                head2,
                "feat(revision2): persist leaderboard",
                ("persistence.py",),
                ("persistence.py",),
                NOW,
            )
            ready = replace(
                reopened,
                tasks=tuple(
                    replace(task, status=TaskStatus.COMPLETED)
                    if task.id == task_id
                    else task
                    for task in reopened.tasks
                ),
                git_commit_results=reopened.git_commit_results + (commit,),
            )
            finalizer_store = MemoryStore(ready)
            verification = ProjectVerificationService(
                clock=Clock(),
                result_id_factory=lambda: "verify-v2",
                environment={"PATH": "/usr/bin:/bin"},
            )
            service = ProjectFinalizationService(
                store=finalizer_store,
                verification=verification,
                supervisor=FakeFinalSupervisor(),
                clock=Clock(),
                event_id_factory=EventIds(),
            )
            final = service.finalize(ready)
            self.assertIs(final.project.status, ProjectStatus.DONE)
            self.assertEqual(len(final.project_verification_results), 2)
            self.assertEqual(
                final.project_verification_results[0].id,
                "verify-v1",
            )
            self.assertEqual(
                final.project_verification_results[-1].id,
                "verify-v2",
            )
            second = final.revisions[-1]
            self.assertEqual(second.revision_number, 2)
            self.assertIs(
                second.lifecycle_status,
                RevisionStatus.COMPLETED,
            )
            self.assertEqual(second.plan_version, 2)
            self.assertEqual(second.completion_head, head2)
            self.assertEqual(
                second.verification_status,
                RevisionCheckStatus.PASS,
            )
            first = final.revisions[0]
            self.assertEqual(first.completion_head, baseline)
            self.assertIs(
                first.verification_status,
                RevisionCheckStatus.PASS,
            )


class FakeFinalSupervisor:
    def final_review(self, request):
        return FinalReviewResult(
            FinalReviewDecision.APPROVE,
            "Approved revision.",
            (),
        )


class SchemaMigrationRevisionTests(unittest.TestCase):
    def legacy_v12(self, state):
        payload = serialize_project_state(state)
        payload["schema_version"] = 12
        payload.pop("revisions")
        for plan in payload["plans"]:
            for key in (
                "base_plan_id",
                "base_plan_version",
                "change_request_id",
                "revision_number",
                "reused_task_ids",
            ):
                plan.pop(key)
        for task in payload["tasks"]:
            for key in ("supersedes_task_id", "derived_from_task_ids"):
                task.pop(key)
        for change in payload["change_requests"]:
            for key in (
                "requested_revision",
                "base_revision",
                "base_plan_id",
                "base_plan_version",
            ):
                change.pop(key)
        return payload

    def test_v12_done_with_evidence_migrates_to_completed_revision_one(self):
        state = done_v1_state(verification=verification_evidence())
        migrated = deserialize_project_state(self.legacy_v12(state))
        self.assertEqual(migrated.project.status, ProjectStatus.DONE)
        self.assertEqual(len(migrated.revisions), 1)
        revision = migrated.revisions[0]
        self.assertEqual(revision.revision_number, 1)
        self.assertIs(revision.lifecycle_status, RevisionStatus.COMPLETED)
        self.assertEqual(revision.plan_version, 1)
        self.assertEqual(revision.completion_head, HEAD1)
        self.assertEqual(revision.verification_status, RevisionCheckStatus.PASS)

    def test_v12_done_without_evidence_does_not_fabricate_pass(self):
        state = done_v1_state()
        migrated = deserialize_project_state(self.legacy_v12(state))
        revision = migrated.revisions[0]
        self.assertIs(revision.lifecycle_status, RevisionStatus.COMPLETED)
        self.assertEqual(
            revision.verification_status,
            RevisionCheckStatus.UNKNOWN,
        )
        self.assertEqual(
            revision.final_review_status,
            RevisionCheckStatus.UNKNOWN,
        )

    def test_v12_running_migrates_to_in_progress_revision_one(self):
        base = done_v1_state()
        state = replace(
            base,
            project=replace(base.project, status=ProjectStatus.RUNNING),
            plans=tuple(
                replace(plan, status=PlanStatus.ACTIVE)
                for plan in base.plans
            ),
        )
        migrated = deserialize_project_state(self.legacy_v12(state))
        self.assertIs(
            migrated.revisions[0].lifecycle_status,
            RevisionStatus.IN_PROGRESS,
        )
        self.assertEqual(
            migrated.revisions[0].verification_status,
            RevisionCheckStatus.NOT_RUN,
        )


if __name__ == "__main__":
    unittest.main()
