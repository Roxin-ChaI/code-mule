from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli import main
from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain import (
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    Milestone,
    Plan,
    PlanStatus,
    ProjectStatus,
    Task,
    TaskStatus,
)
from code_mule.human import request_human_action
from code_mule.progress import ConsoleProgressRenderer
from code_mule.runtime import (
    ProjectExecutionConfig,
    ProjectExecutionService,
    TaskCycleConfig,
    TaskCycleService,
    TaskPromptBuilder,
)
from code_mule.scheduler import TaskScheduler
from code_mule.state.store import JsonProjectStateStore
from code_mule.worker import CodexApprovalRequired


class FakePlanning:
    def __init__(self, store): self.store = store

    def plan(self, request):
        state = self.store.load()
        now = datetime.now(UTC)
        plan = Plan("PLAN-1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), now)
        milestone = Milestone("M1", plan.id, "Gate", "active", ("TASK-1",))
        task = Task(
            "TASK-1", "M1", "Request approval", "Fake gated work",
            TaskStatus.PENDING, (), ("approval is persisted",), 0, now, now,
        )
        self.store.save(
            replace(
                state,
                project=replace(
                    state.project,
                    status=ProjectStatus.RUNNING,
                    active_plan_id=plan.id,
                    updated_at=now,
                ),
                plans=(plan,),
                milestones=(milestone,),
                tasks=(task,),
            )
        )
        return type("Planning", (), {"ready_for_execution": True, "plan_id": plan.id})()


class FakeApprovalSession:
    def start(self): pass
    def execute(self, request, *, report_id, created_at):
        raise CodexApprovalRequired("sensitive-worker-detail")
    def close(self): pass


class NoReview:
    def review(self, request):
        raise AssertionError("approval request must stop before Supervisor review")


class NoChange:
    def apply_and_resume(self, request):
        raise AssertionError("change is outside this E2E")


class HumanResolutionCliE2E(unittest.TestCase):
    def test_fake_worker_approval_inspect_approve_and_reject(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            state_file = root / "project.json"
            store = JsonProjectStateStore(state_file)
            sequence = count(1)
            progress = StringIO()

            def runtime_factory(state):
                renderer = ConsoleProgressRenderer(progress)

                def cycle_factory():
                    return TaskCycleService(
                        worker_session_factory=FakeApprovalSession,
                        supervisor=NoReview(),
                        store=store,
                        clock=lambda: datetime.now(UTC),
                        report_id_factory=lambda: f"report-{next(sequence)}",
                        decision_id_factory=lambda: f"decision-{next(sequence)}",
                        event_id_factory=lambda: f"event-{next(sequence)}",
                        config=TaskCycleConfig(max_attempts=2),
                        progress_sink=renderer,
                    )

                execution = ProjectExecutionService(
                    store=store,
                    scheduler=TaskScheduler(),
                    task_cycle_factory=cycle_factory,
                    prompt_builder=TaskPromptBuilder(),
                    clock=lambda: datetime.now(UTC),
                    event_id_factory=lambda: f"project-event-{next(sequence)}",
                    config=ProjectExecutionConfig(max_tasks_per_run=5),
                    progress_sink=renderer,
                )
                return RuntimeComposition(
                    supervisor=NoReview(),
                    worker_service=object(),
                    planning=FakePlanning(store),
                    execution=execution,
                    change_execution=NoChange(),
                    renderer=renderer,
                )

            def factory(path, environment, stdout, stderr):
                return ProductionCliComposition(
                    path,
                    environment=environment,
                    stdout=stdout,
                    stderr=stderr,
                    runtime_factory=runtime_factory,
                )

            def invoke(arguments):
                output, errors = StringIO(), StringIO()
                code = main(
                    arguments + ["--state-file", str(state_file)],
                    composition_factory=factory,
                    environment={},
                    stdout=output,
                    stderr=errors,
                )
                return code, output.getvalue(), errors.getvalue()

            code, _, _ = invoke(
                [
                    "init", "--project-id", "project-1", "--name", "Project",
                    "--workspace", str(workspace),
                ]
            )
            self.assertEqual(code, 0)
            code, output, errors = invoke(["run", "--objective", "Trigger gate"])
            self.assertEqual(code, 4)
            self.assertNotIn("sensitive-worker-detail", output + errors)

            gated = store.load()
            action = gated.human_actions[0]
            self.assertIs(action.category, HumanActionCategory.WORKER_APPROVAL)
            self.assertIs(action.status, HumanActionStatus.PENDING)
            code, _, errors = invoke(["resume"])
            self.assertEqual(code, 4)
            self.assertIn("cannot bypass HUMAN_REQUIRED", errors)
            before = state_file.read_bytes()
            code, output, _ = invoke(["inspect", "--verbose"])
            self.assertEqual(code, 0)
            self.assertIn(action.id, output)
            self.assertEqual(state_file.read_bytes(), before)

            code, output, _ = invoke(["approve", action.id])
            self.assertEqual(code, 0)
            self.assertIn("Worker session recovery is not available", output)
            approved = store.load()
            self.assertIs(approved.project.status, ProjectStatus.HUMAN_REQUIRED)
            self.assertIs(approved.human_actions[0].status, HumanActionStatus.APPROVED)
            self.assertIs(
                approved.human_resolutions[0].strategy,
                HumanResolutionStrategy.APPROVE,
            )
            self.assertEqual(approved.events[-1].event_type, "human_action.approved")

            ids = iter(("reject-source", "reject-requested"))
            second = request_human_action(
                approved,
                category=HumanActionCategory.EXTERNAL_SIDE_EFFECT,
                summary="Fake remote mutation",
                requested_action="Reject this exact operation",
                risk="Remote state mutation",
                task_id="TASK-1",
                operation_time=datetime.now(UTC),
                action_id="action-reject",
                event_id_factory=lambda: next(ids),
                source_event_types=("task.human_required",),
            )
            store.save(second)
            code, _, _ = invoke(["reject", "action-reject"])
            self.assertEqual(code, 0)
            rejected = store.load()
            self.assertIs(rejected.human_actions[-1].status, HumanActionStatus.REJECTED)
            self.assertIs(
                rejected.human_resolutions[-1].strategy,
                HumanResolutionStrategy.REJECT,
            )
            self.assertEqual(rejected.events[-1].event_type, "human_action.rejected")


if __name__ == "__main__":
    unittest.main()
