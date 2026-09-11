import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from code_mule.domain import (
    HumanActionCategory,
    PlanStatus,
    ProjectRevision,
    ProjectStatus,
    RevisionCheckStatus,
    RevisionStatus,
    TaskStatus,
)
from code_mule.git_delivery import GitCommitResult
from code_mule.progress import ProgressEventType, RecordingProgressSink
from code_mule.orchestrator import OrchestratorService, StopCommand
from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCommand,
    ProjectVerificationSpec,
    ProjectVerificationStatus,
)
from code_mule.project_verification.service import (
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.supervisor import FinalReviewResult, SupervisorService

from state import make_project_state


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def git(root, *arguments):
    return subprocess.run(
        ("git", *arguments), cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class MemoryStore:
    def __init__(self, state):
        self.state = state
        self.saved = []

    def load(self):
        return self.state

    def save(self, state):
        self.state = state
        self.saved.append(state)


class FakeFinalSupervisor:
    def __init__(self, decision=FinalReviewDecision.APPROVE, error=None, issues=()):
        self.decision = decision
        self.error = error
        self.issues = issues
        self.requests = []

    def final_review(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FinalReviewResult(
            self.decision, "Deterministic final review.", self.issues
        )


class InvalidFinalReviewClient:
    def __init__(self):
        self.calls = 0

    def create_structured_response(self, **kwargs):
        self.calls += 1
        return {"decision": "rework", "rationale": "invalid", "issues": []}


class FinalVerificationCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Verifier")
        git(self.root, "config", "user.email", "verifier@example.invalid")
        (self.root / "app.py").write_text("VALUE = 1\n")
        git(self.root, "add", "--", "app.py")
        git(self.root, "commit", "-q", "-m", "task delivery")
        self.head = git(self.root, "rev-parse", "HEAD")

    def tearDown(self):
        self.temporary.cleanup()

    def state(self, *, command=("true",), category=ProjectVerificationCategory.TEST):
        source = make_project_state()
        task = replace(source.tasks[0], status=TaskStatus.COMPLETED)
        plan = replace(source.plans[0], status=PlanStatus.ACTIVE)
        result = GitCommitResult(
            task.id, str(self.root), "a" * 40, self.head, "feat(task): delivery",
            ("app.py",), ("app.py",), NOW,
        )
        spec = ProjectVerificationSpec(
            source.project.id,
            (
                ProjectVerificationCommand(
                    category.value, category, tuple(command), timeout_seconds=5
                ),
            ),
        )
        return replace(
            source,
            project=replace(
                source.project,
                status=ProjectStatus.RUNNING,
                current_task_id=None,
                workspace=str(self.root),
                objective="Deliver the verified application",
            ),
            plans=(plan,),
            tasks=(task,),
            git_commit_results=(result,),
            project_verification_spec=spec,
            project_verification_results=(),
            human_actions=(),
        )

    def finalizer(self, state, supervisor, runner=None, progress=None):
        store = MemoryStore(state)
        verification = ProjectVerificationService(
            clock=lambda: NOW,
            result_id_factory=lambda: "verification-1",
            runner=runner or (lambda command, **kwargs: (0, False)),
            environment={"PATH": "/usr/bin:/bin"},
        )
        service = ProjectFinalizationService(
            store=store,
            verification=verification,
            supervisor=supervisor,
            clock=lambda: NOW,
            event_id_factory=iter((f"event-{index}" for index in range(30))).__next__,
            progress_sink=progress,
        )
        return service, store


class ProjectVerificationServiceTests(FinalVerificationCase):
    def test_configured_checks_and_git_pass_without_retaining_output(self):
        state = self.state()
        calls = []

        def runner(command, **kwargs):
            calls.append((tuple(command), kwargs))
            return 0, False

        result = ProjectVerificationService(
            clock=lambda: NOW,
            result_id_factory=lambda: "verification-1",
            runner=runner,
            environment={"PATH": "/usr/bin", "DEEPSEEK_API_KEY": "secret"},
        ).run(state)
        self.assertTrue(result.passed)
        self.assertEqual(calls[0][0], ("true",))
        self.assertNotIn("DEEPSEEK_API_KEY", calls[0][1]["environment"])
        self.assertIs(result.checks[-1].category, ProjectVerificationCategory.GIT_CLEAN)
        self.assertIs(result.checks[-1].status, ProjectVerificationStatus.PASS)

    def test_test_lint_failure_and_timeout_are_typed(self):
        scenarios = (
            (ProjectVerificationCategory.TEST, (1, False), ProjectVerificationStatus.FAIL),
            (ProjectVerificationCategory.LINT, (2, False), ProjectVerificationStatus.FAIL),
            (ProjectVerificationCategory.TEST, (0, True), ProjectVerificationStatus.TIMEOUT),
        )
        for category, runner_result, expected in scenarios:
            with self.subTest(category=category, expected=expected):
                result = ProjectVerificationService(
                    clock=lambda: NOW,
                    result_id_factory=lambda: "verification-1",
                    runner=lambda command, **kwargs: runner_result,
                ).run(self.state(category=category))
                self.assertFalse(result.passed)
                self.assertIs(result.checks[0].status, expected)
                self.assertNotIn("stdout", result.checks[0].safe_summary)

    def test_dirty_git_fails_final_check(self):
        state = self.state()
        (self.root / "unknown.txt").write_text("external\n")
        result = ProjectVerificationService(
            clock=lambda: NOW,
            result_id_factory=lambda: "verification-1",
            runner=lambda command, **kwargs: (0, False),
        ).run(state)
        self.assertFalse(result.passed)
        self.assertIs(result.checks[-1].status, ProjectVerificationStatus.FAIL)


class ProjectFinalizationTests(FinalVerificationCase):
    def test_stop_during_checks_prevents_final_review_and_done(self):
        supervisor = FakeFinalSupervisor()
        holder = {}

        def runner(command, **kwargs):
            store = holder["store"]
            OrchestratorService(
                store,
                clock=lambda: NOW,
                event_id_factory=iter((f"cancel-{index}" for index in range(4))).__next__,
            ).stop(StopCommand(store.load().project.id))
            return 0, False

        service, store = self.finalizer(self.state(), supervisor, runner=runner)
        holder["store"] = store
        final = service.finalize(store.state)
        self.assertIs(final.project.status, ProjectStatus.CANCELLED)
        self.assertEqual(supervisor.requests, [])
        self.assertNotIn("project.completed", (event.event_type for event in final.events))

    def test_unfinished_reentry_does_not_reuse_stale_git_evidence(self):
        state = self.state()
        supervisor = FakeFinalSupervisor()
        service, store = self.finalizer(state, supervisor)
        first = service._verification.run(state)
        first = replace(
            first,
            id="verification-old",
            final_review_decision=FinalReviewDecision.APPROVE,
            final_review_summary="Previously approved.",
        )
        store.state = replace(state, project_verification_results=(first,))
        (self.root / "external.txt").write_text("changed after verification\n")

        final = service.finalize(store.state)

        self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(len(final.project_verification_results), 2)
        self.assertFalse(final.project_verification_results[-1].passed)
        self.assertEqual(supervisor.requests, [])

    def test_all_pass_and_final_approve_is_only_done_boundary(self):
        state = self.state()
        self.assertIs(state.project.status, ProjectStatus.RUNNING)
        supervisor = FakeFinalSupervisor()
        progress = RecordingProgressSink()
        service, store = self.finalizer(state, supervisor, progress=progress)
        final = service.finalize(state)
        self.assertIs(final.project.status, ProjectStatus.DONE)
        self.assertEqual(len(final.project_verification_results), 1)
        result = final.project_verification_results[0]
        self.assertTrue(result.passed)
        self.assertIs(result.final_review_decision, FinalReviewDecision.APPROVE)
        events = tuple(item.event_type for item in final.events)
        for expected in (
            "project.verification_started",
            "project.verification_completed",
            "project.final_review_started",
            "project.final_review_completed",
            "project.completed",
        ):
            self.assertIn(expected, events)
        self.assertEqual(len(supervisor.requests), 1)
        self.assertEqual(
            tuple(event.type for event in progress.events),
            (
                ProgressEventType.PROJECT_VERIFICATION_STARTED,
                ProgressEventType.PROJECT_VERIFICATION_COMPLETED,
                ProgressEventType.PROJECT_FINAL_REVIEW_STARTED,
                ProgressEventType.PROJECT_FINAL_REVIEW_COMPLETED,
            ),
        )

        before = len(final.project_verification_results)
        self.assertIs(service.finalize(final), final)
        self.assertEqual(len(store.state.project_verification_results), before)
        self.assertEqual(len(supervisor.requests), 1)

    def test_failed_check_never_calls_final_review_and_requires_human(self):
        supervisor = FakeFinalSupervisor()
        service, store = self.finalizer(
            self.state(), supervisor, runner=lambda command, **kwargs: (1, False)
        )
        final = service.finalize(store.state)
        self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(supervisor.requests, [])
        self.assertEqual(len(final.project_verification_results), 1)

    def test_final_reject_and_failure_require_human(self):
        for supervisor in (
            FakeFinalSupervisor(FinalReviewDecision.HUMAN_REQUIRED),
            FakeFinalSupervisor(error=RuntimeError("unavailable")),
        ):
            with self.subTest(supervisor=supervisor):
                service, store = self.finalizer(self.state(), supervisor)
                final = service.finalize(store.state)
                self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
                self.assertEqual(len(final.human_actions), 1)
                self.assertIs(
                    final.human_actions[0].category,
                    HumanActionCategory.FINAL_REVIEW_DECISION
                    if supervisor.error is None
                    else HumanActionCategory.SUPERVISOR_FAILURE,
                )

    def test_human_judgment_persists_only_bounded_structured_findings(self):
        supervisor = FakeFinalSupervisor(
            FinalReviewDecision.HUMAN_REQUIRED,
            issues=(
                "Acceptance criterion REQ-1 needs Boss judgment. "
                "token=top-secret-value",
            ),
        )
        state = self.state()
        service, store = self.finalizer(state, supervisor)

        final = service.finalize(store.state)

        action = final.human_actions[-1]
        source = next(
            event
            for event in final.events
            if event.event_type == "project.final_review_human_judgment"
        )
        self.assertIs(action.category, HumanActionCategory.FINAL_REVIEW_DECISION)
        self.assertEqual(
            source.metadata["issue_1"],
            "Acceptance criterion REQ-1 needs Boss judgment. [REDACTED]",
        )
        self.assertNotIn("top-secret-value", str(final.events))
        self.assertNotIn("top-secret-value", str(final.human_actions))
        self.assertNotIn("raw_response", source.metadata)
        self.assertNotIn("reasoning", source.metadata)

    def test_human_judgment_records_revision_evidence_without_mutating_history(self):
        state = self.state()
        revision_one = ProjectRevision(
            1,
            NOW,
            lifecycle_status=RevisionStatus.COMPLETED,
            plan_id="historical-plan",
            plan_version=1,
            completed_at=NOW,
            completion_head="b" * 40,
            verification_status=RevisionCheckStatus.PASS,
            final_review_status=RevisionCheckStatus.PASS,
            verification_result_id="historical-verification",
        )
        revision_two = ProjectRevision(
            2,
            NOW,
            plan_id=state.project.active_plan_id,
            plan_version=2,
            base_revision=1,
        )
        state = replace(
            state,
            plans=(replace(state.plans[0], version=2),),
            revisions=(revision_one, revision_two),
        )
        service, store = self.finalizer(
            state,
            FakeFinalSupervisor(FinalReviewDecision.HUMAN_REQUIRED),
        )

        final = service.finalize(store.state)

        self.assertEqual(final.revisions[0], revision_one)
        self.assertIs(
            final.revisions[1].verification_status,
            RevisionCheckStatus.PASS,
        )
        self.assertIs(
            final.revisions[1].final_review_status,
            RevisionCheckStatus.UNKNOWN,
        )
        self.assertEqual(
            final.revisions[1].verification_result_id,
            final.project_verification_results[-1].id,
        )

    def test_malformed_final_review_exhausts_bounded_retry_then_requires_human(self):
        client = InvalidFinalReviewClient()
        supervisor = SupervisorService(client)
        service, store = self.finalizer(self.state(), supervisor)

        final = service.finalize(store.state)

        self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(supervisor.last_attempt_results), 2)
        self.assertEqual(len(final.project_verification_results), 1)
        self.assertIsNone(
            final.project_verification_results[0].final_review_decision
        )
        failure_event = next(
            event
            for event in final.events
            if event.event_type == "project.final_review_failed"
        )
        self.assertEqual(failure_event.metadata["operation"], "final_review")
        self.assertEqual(failure_event.metadata["attempt_count"], "2")
        self.assertEqual(
            failure_event.metadata["failure_category"],
            "decision_contract_violation",
        )


if __name__ == "__main__":
    unittest.main()
