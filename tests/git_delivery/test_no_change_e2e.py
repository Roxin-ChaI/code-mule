import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from code_mule.domain import PlanStatus, SupervisorDecisionType, TaskStatus
from code_mule.diagnosis import DiagnosisStage, ProjectDiagnosisService
from code_mule.git_delivery import GitCommitResult, GitDeliveryService
from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCommand,
    ProjectVerificationSpec,
)
from code_mule.project_verification.service import (
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.presentation import render_human_action, render_project_diagnosis
from code_mule.runtime import TaskCycleRequest
from code_mule.supervisor import FinalReviewResult

from runtime.test_cycle import (
    FakeStore,
    FakeSupervisor,
    FakeWorkerSession,
    build_cycle,
    cycle_state,
    review,
)
from human.test_service import no_change_gate


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


class VerificationOnlyWorker(FakeWorkerSession):
    def execute(self, request, *, report_id, created_at):
        report = super().execute(request, report_id=report_id, created_at=created_at)
        return replace(
            report,
            files_changed=(),
            git_state="clean",
            summary="required verification completed without repository changes",
        )


class FinalSupervisor:
    def __init__(self):
        self.requests = []

    def final_review(self, request):
        self.requests.append(request)
        return FinalReviewResult(
            FinalReviewDecision.APPROVE,
            "Deterministic final review approved the verified project.",
            (),
        )


class VerificationOnlyLifecycleE2ETests(unittest.TestCase):
    def test_legacy_gate_inspect_and_diagnosis_expose_safe_exact_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = no_change_gate(root)
            action = state.human_actions[-1]

            inspected = "\n".join(
                render_human_action(action, state=state, verbose=True)
            )
            diagnosis = ProjectDiagnosisService().diagnose(state)
            diagnosed = "\n".join(render_project_diagnosis(diagnosis))

            self.assertIn("NO-CHANGE DELIVERY REVIEW", inspected)
            self.assertIn("Reported changes  None", inspected)
            self.assertIn("Actual unstaged   None", inspected)
            self.assertIn("Actual staged     None", inspected)
            self.assertIn("Commit created    No", inspected)
            self.assertIn("Supervisor review Not started", inspected)
            self.assertIn("continue_after_report", inspected)
            self.assertIs(diagnosis.blocker_stage, DiagnosisStage.GIT_DELIVERY)
            self.assertIn("Git delivery", diagnosed)
            self.assertIn("Recoverable", diagnosed)
            self.assertEqual(git(root, "status", "--short"), "")

    def test_two_changed_tasks_then_verification_only_task_reaches_done(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-q")
            git(root, "config", "user.name", "Code Mule Test")
            git(root, "config", "user.email", "code-mule@example.invalid")
            (root / "README.md").write_text("baseline\n", encoding="utf-8")
            git(root, "add", "--", "README.md")
            git(root, "commit", "-q", "-m", "initial")
            initial = git(root, "rev-parse", "HEAD")

            (root / "task-1.txt").write_text("task one\n", encoding="utf-8")
            git(root, "add", "--", "task-1.txt")
            git(root, "commit", "-q", "-m", "feat(task): task one")
            task_one_head = git(root, "rev-parse", "HEAD")
            (root / "task-2.txt").write_text("task two\n", encoding="utf-8")
            git(root, "add", "--", "task-2.txt")
            git(root, "commit", "-q", "-m", "feat(task): task two")
            task_two_head = git(root, "rev-parse", "HEAD")

            source = cycle_state()
            template = source.tasks[0]
            task_one = replace(
                template,
                id="TASK-001",
                title="Implement feature",
                status=TaskStatus.COMPLETED,
                dependencies=(),
                execution_attempts=1,
            )
            task_two = replace(
                template,
                id="TASK-002",
                title="Add tests",
                status=TaskStatus.COMPLETED,
                dependencies=(task_one.id,),
                execution_attempts=1,
            )
            task_three = replace(
                template,
                id="TASK-003",
                title="Run final local verification",
                status=TaskStatus.IN_PROGRESS,
                dependencies=(task_two.id,),
                execution_attempts=0,
            )
            milestone = replace(
                source.milestones[0],
                task_ids=(task_one.id, task_two.id, task_three.id),
            )
            state = replace(
                source,
                project=replace(
                    source.project,
                    current_task_id=task_three.id,
                    workspace=str(root),
                    objective="Deliver and verify the project",
                ),
                plans=(replace(source.plans[0], status=PlanStatus.ACTIVE),),
                milestones=(milestone,),
                tasks=(task_one, task_two, task_three),
                decisions=(),
                execution_reports=(),
                events=(),
                git_baselines=(),
                git_change_sets=(),
                git_commit_results=(
                    GitCommitResult(
                        task_one.id,
                        str(root),
                        initial,
                        task_one_head,
                        "feat(task): task one",
                        ("task-1.txt",),
                        ("task-1.txt",),
                        NOW,
                    ),
                    GitCommitResult(
                        task_two.id,
                        str(root),
                        task_one_head,
                        task_two_head,
                        "feat(task): task two",
                        ("task-2.txt",),
                        ("task-2.txt",),
                        NOW,
                    ),
                ),
                execution_attempts=(),
                latest_execution_stop=None,
                latest_safe_point=None,
                project_verification_spec=ProjectVerificationSpec(
                    source.project.id,
                    (
                        ProjectVerificationCommand(
                            "Tests",
                            ProjectVerificationCategory.TEST,
                            ("python", "-m", "unittest"),
                            timeout_seconds=5,
                        ),
                    ),
                ),
                project_verification_results=(),
                human_actions=(),
            )
            store = FakeStore(state)
            delivery = GitDeliveryService(root, clock=lambda: NOW)
            supervisor = FakeSupervisor(
                [review(SupervisorDecisionType.CONTINUE)]
            )
            cycle, request, _, _, _, _ = build_cycle(
                store=store,
                session=VerificationOnlyWorker(),
                supervisor=supervisor,
                git_delivery=delivery,
            )
            request = TaskCycleRequest(task_three, "Run the required checks only")

            outcome = cycle.execute(request)

            self.assertFalse(outcome.human_action_required)
            self.assertEqual(git(root, "rev-parse", "HEAD"), task_two_head)
            self.assertEqual(git(root, "rev-list", "--count", "HEAD"), "3")
            self.assertEqual(len(store.current.git_commit_results), 2)
            self.assertTrue(all(task.status is TaskStatus.COMPLETED for task in store.current.tasks))
            self.assertIn(
                "git.no_commit_required",
                tuple(event.event_type for event in store.current.events),
            )

            final_supervisor = FinalSupervisor()
            finalizer = ProjectFinalizationService(
                store=store,
                verification=ProjectVerificationService(
                    clock=lambda: NOW,
                    result_id_factory=lambda: "verification-1",
                    runner=lambda command, **kwargs: (0, False),
                    environment={"PATH": "/usr/bin:/bin"},
                ),
                supervisor=final_supervisor,
                clock=lambda: NOW,
                event_id_factory=iter(
                    f"final-event-{index}" for index in range(20)
                ).__next__,
            )

            final = finalizer.finalize(store.current)

            self.assertEqual(final.project.status.value, "done")
            self.assertEqual(len(final_supervisor.requests), 1)
            self.assertEqual(git(root, "status", "--short"), "")
            self.assertEqual(git(root, "rev-list", "--count", "HEAD"), "3")
            self.assertEqual(len(final.git_commit_results), 2)


if __name__ == "__main__":
    unittest.main()
