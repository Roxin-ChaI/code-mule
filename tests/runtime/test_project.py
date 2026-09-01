import unittest
from dataclasses import replace
from datetime import UTC, datetime

from code_mule.domain.enums import (
    PlanStatus,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import Milestone, Plan, Project, Task
from code_mule.progress import (
    ProgressEventType,
    RecordingProgressSink,
)
from code_mule.runtime import (
    InvalidProjectExecutionState,
    ProjectExecutionConfig,
    ProjectExecutionService,
    ProjectExecutionStopReason,
    TaskCycleOutcome,
    TaskPromptBuilder,
)
from code_mule.scheduler import DependencyCycleDetected, TaskScheduler
from code_mule.scheduler.errors import UnknownTaskDependency
from code_mule.state.models import ProjectState


NOW = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)


def make_task(
    task_id,
    *,
    milestone_id="milestone-1",
    status=TaskStatus.PENDING,
    dependencies=(),
    title=None,
    description=None,
):
    return Task(
        id=task_id,
        milestone_id=milestone_id,
        title=title or f"Title {task_id}",
        description=description or f"Description {task_id}",
        status=status,
        dependencies=dependencies,
        acceptance_criteria=(f"Accept {task_id}",),
        execution_attempts=0,
        created_at=NOW,
        updated_at=NOW,
    )


def make_state(
    tasks,
    *,
    milestone_specs=None,
    project_status=ProjectStatus.RUNNING,
    current_task_id=None,
):
    tasks = tuple(tasks)
    if milestone_specs is None:
        milestone_specs = (("milestone-1", tuple(task.id for task in tasks)),)
    milestones = tuple(
        Milestone(
            id=milestone_id,
            plan_id="plan-1",
            title=f"Milestone {milestone_id}",
            status=status,
            task_ids=task_ids,
        )
        for milestone_id, task_ids, *optional_status in milestone_specs
        for status in (optional_status[0] if optional_status else "pending",)
    )
    plan = Plan(
        id="plan-1",
        project_id="project-1",
        version=1,
        status=PlanStatus.ACTIVE,
        requirement_ids=(),
        milestone_ids=tuple(item.id for item in milestones),
        created_at=NOW,
    )
    project = Project(
        id="project-1",
        name="Project Alpha",
        status=project_status,
        active_plan_id=plan.id,
        current_task_id=current_task_id,
        created_at=NOW,
        updated_at=NOW,
    )
    return ProjectState(
        project=project,
        requirements=(),
        plans=(plan,),
        milestones=milestones,
        tasks=tasks,
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


class FakeStore:
    def __init__(self, state, *, fail_on_save=None):
        self.current = state
        self.saved = []
        self.fail_on_save = fail_on_save

    def load(self):
        return self.current

    def save(self, state):
        if len(self.saved) + 1 == self.fail_on_save:
            raise OSError("persistence failed")
        self.saved.append(state)
        self.current = state


class IdFactory:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"project-event-{self.value}"


class FakeCycleFactory:
    def __init__(self, store, *, human_on=(), boundary_statuses=None):
        self.store = store
        self.human_on = set(human_on)
        self.boundary_statuses = dict(boundary_statuses or {})
        self.requests = []
        self.instances = 0

    def __call__(self):
        self.instances += 1
        return self

    def execute(self, request):
        self.requests.append(request)
        state = self.store.load()
        if request.task.id in self.human_on:
            status = ProjectStatus.HUMAN_REQUIRED
            task = next(item for item in state.tasks if item.id == request.task.id)
            self.store.save(
                replace(
                    state,
                    project=replace(state.project, status=status),
                )
            )
            return TaskCycleOutcome(
                request.task.id,
                1,
                SupervisorDecisionType.HUMAN_REQUIRED,
                (),
                (),
                None,
                True,
            )

        completed = replace(request.task, status=TaskStatus.COMPLETED)
        updated = replace(
            state,
            project=replace(state.project, current_task_id=None),
            tasks=tuple(
                completed if task.id == completed.id else task
                for task in state.tasks
            ),
        )
        boundary_status = self.boundary_statuses.get(request.task.id)
        if boundary_status is not None:
            updated = replace(
                updated,
                project=replace(updated.project, status=boundary_status),
            )
        self.store.save(updated)
        return TaskCycleOutcome(
            request.task.id,
            1,
            SupervisorDecisionType.CONTINUE,
            (),
            (),
            None,
            False,
        )


def build_service(
    state,
    *,
    max_tasks=20,
    cycle_options=None,
    store=None,
    progress_sink=None,
):
    store = store or FakeStore(state)
    cycles = FakeCycleFactory(store, **(cycle_options or {}))
    service = ProjectExecutionService(
        store=store,
        scheduler=TaskScheduler(),
        task_cycle_factory=cycles,
        prompt_builder=TaskPromptBuilder(),
        clock=lambda: NOW,
        event_id_factory=IdFactory(),
        config=ProjectExecutionConfig(max_tasks),
        progress_sink=progress_sink,
    )
    return service, store, cycles


class ProjectExecutionContractTests(unittest.TestCase):
    def test_config_requires_positive_task_limit(self):
        self.assertEqual(ProjectExecutionConfig(1).max_tasks_per_run, 1)
        for value in (0, -1):
            with self.assertRaises(ValueError):
                ProjectExecutionConfig(value)

    def test_non_running_boundaries_do_not_execute(self):
        expected = {
            ProjectStatus.PAUSED_BY_BOSS: ProjectExecutionStopReason.PAUSED,
            ProjectStatus.CHANGE_REQUESTED: ProjectExecutionStopReason.CHANGE_REQUESTED,
            ProjectStatus.HUMAN_REQUIRED: ProjectExecutionStopReason.HUMAN_REQUIRED,
            ProjectStatus.DONE: ProjectExecutionStopReason.PLAN_COMPLETED,
            ProjectStatus.FAILED: ProjectExecutionStopReason.TASK_CYCLE_STOPPED,
        }
        for status, reason in expected.items():
            with self.subTest(status=status):
                service, store, cycles = build_service(
                    make_state((make_task("task-a"),), project_status=status)
                )
                outcome = service.run()
                self.assertIs(outcome.stop_reason, reason)
                self.assertEqual(cycles.requests, [])
                self.assertEqual(store.saved, [])

    def test_other_project_states_fail_closed(self):
        service, _, cycles = build_service(
            make_state((make_task("task-a"),), project_status=ProjectStatus.PLANNING)
        )
        with self.assertRaises(InvalidProjectExecutionState):
            service.run()
        self.assertEqual(cycles.requests, [])

    def test_prompt_is_bounded_to_current_task_and_completed_direct_dependencies(self):
        dependency = make_task(
            "dep", status=TaskStatus.COMPLETED, title="Dependency identity"
        )
        current = make_task("current", dependencies=("dep",))
        unrelated = make_task(
            "unrelated", title="SECRET UNRELATED", description="API_KEY=never"
        )
        state = make_state((dependency, current, unrelated))
        prompt = TaskPromptBuilder().build(state, current)
        self.assertIn("Project Alpha", prompt)
        self.assertIn("Task ID: current", prompt)
        self.assertIn("Accept current", prompt)
        self.assertIn("dep: Dependency identity", prompt)
        self.assertIn("native output schema", prompt)
        self.assertNotIn("SECRET UNRELATED", prompt)
        self.assertNotIn("API_KEY=never", prompt)


class ProjectExecutionFlowTests(unittest.TestCase):
    def test_project_progress_events_preserve_required_execution_order(self):
        state = make_state(
            (
                make_task("task-a"),
                make_task("task-b", dependencies=("task-a",)),
            )
        )
        progress = RecordingProgressSink()
        service, _, _ = build_service(state, progress_sink=progress)
        service.run()
        self.assertEqual(
            tuple(event.type for event in progress.events),
            (
                ProgressEventType.PROJECT_STARTED,
                ProgressEventType.TASK_DISPATCHED,
                ProgressEventType.TASK_COMPLETED,
                ProgressEventType.TASK_DISPATCHED,
                ProgressEventType.TASK_COMPLETED,
                ProgressEventType.PLAN_COMPLETED,
                ProgressEventType.PROJECT_COMPLETED,
            ),
        )
        task_events = tuple(
            event
            for event in progress.events
            if event.type is ProgressEventType.TASK_COMPLETED
        )
        self.assertEqual(
            tuple(event.metadata["completed_tasks"] for event in task_events),
            ("1", "2"),
        )
        self.assertEqual(
            tuple(event.metadata["total_tasks"] for event in task_events),
            ("2", "2"),
        )

    def test_broken_progress_sink_does_not_stop_project_execution(self):
        class BrokenProgressSink:
            def emit(self, event):
                raise RuntimeError("presentation failed")

        state = make_state((make_task("task-a"),))
        service, store, cycles = build_service(
            state, progress_sink=BrokenProgressSink()
        )
        outcome = service.run()
        self.assertIs(
            outcome.stop_reason, ProjectExecutionStopReason.PLAN_COMPLETED
        )
        self.assertIs(store.current.project.status, ProjectStatus.DONE)
        self.assertEqual(len(cycles.requests), 1)
        self.assertGreaterEqual(len(service.progress_errors), 1)

    def test_dependency_chain_completes_tasks_milestone_plan_and_project(self):
        original = make_state(
            (
                make_task("task-a"),
                make_task("task-b", dependencies=("task-a",)),
            )
        )
        service, store, cycles = build_service(original)
        outcome = service.run()
        self.assertEqual(outcome.task_ids, ("task-a", "task-b"))
        self.assertEqual(outcome.tasks_started, 2)
        self.assertEqual(outcome.tasks_completed, 2)
        self.assertTrue(outcome.plan_completed)
        self.assertIs(
            outcome.stop_reason, ProjectExecutionStopReason.PLAN_COMPLETED
        )
        self.assertIs(store.current.project.status, ProjectStatus.DONE)
        self.assertIs(store.current.plans[0].status, PlanStatus.COMPLETED)
        self.assertEqual(store.current.milestones[0].status, "completed")
        self.assertEqual(
            tuple(task.status for task in store.current.tasks),
            (TaskStatus.COMPLETED, TaskStatus.COMPLETED),
        )
        self.assertEqual(cycles.instances, 2)
        self.assertEqual(original.project.status, ProjectStatus.RUNNING)
        event_types = tuple(event.event_type for event in store.current.events)
        self.assertEqual(event_types.count("milestone.completed"), 1)
        self.assertEqual(event_types[-2:], ("plan.completed", "project.completed"))
        self.assertNotIn("Execute this Code Mule task", repr(store.current.events))

    def test_completed_task_is_skipped_and_next_task_executes(self):
        state = make_state(
            (
                make_task("task-a", status=TaskStatus.COMPLETED),
                make_task("task-b"),
            )
        )
        outcome = build_service(state)[0].run()
        self.assertEqual(outcome.task_ids, ("task-b",))

    def test_blocked_dependency_enters_human_required(self):
        state = make_state(
            (
                make_task("task-a", status=TaskStatus.BLOCKED),
                make_task("task-b", dependencies=("task-a",)),
            )
        )
        service, store, cycles = build_service(state)
        outcome = service.run()
        self.assertIs(
            outcome.stop_reason, ProjectExecutionStopReason.NO_RUNNABLE_TASK
        )
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(cycles.requests, [])
        self.assertEqual(store.current.events[-1].event_type, "project.no_runnable_task")

    def test_cancelled_dependency_does_not_satisfy_dependent_task(self):
        state = make_state(
            (
                make_task("task-a", status=TaskStatus.CANCELLED),
                make_task("task-b", dependencies=("task-a",)),
            )
        )
        service, _, cycles = build_service(state)
        outcome = service.run()
        self.assertIs(
            outcome.stop_reason, ProjectExecutionStopReason.NO_RUNNABLE_TASK
        )
        self.assertEqual(cycles.requests, [])

    def test_invalid_dependency_graph_never_calls_task_cycle(self):
        cases = (
            (
                make_state((make_task("task-a", dependencies=("missing",)),)),
                UnknownTaskDependency,
            ),
            (
                make_state(
                    (
                        make_task("task-a", dependencies=("task-b",)),
                        make_task("task-b", dependencies=("task-a",)),
                    )
                ),
                DependencyCycleDetected,
            ),
        )
        for state, error in cases:
            with self.subTest(error=error):
                service, store, cycles = build_service(state)
                with self.assertRaises(error):
                    service.run()
                self.assertEqual(cycles.requests, [])
                self.assertEqual(store.saved, [])

    def test_task_cycle_human_required_stops_immediately(self):
        state = make_state((make_task("task-a"), make_task("task-b")))
        service, store, cycles = build_service(
            state, cycle_options={"human_on": ("task-a",)}
        )
        outcome = service.run()
        self.assertEqual(outcome.task_ids, ("task-a",))
        self.assertIs(
            outcome.stop_reason, ProjectExecutionStopReason.HUMAN_REQUIRED
        )
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(tuple(item.task.id for item in cycles.requests), ("task-a",))

    def test_change_and_pause_are_observed_at_task_boundary(self):
        for status, reason in (
            (
                ProjectStatus.CHANGE_REQUESTED,
                ProjectExecutionStopReason.CHANGE_REQUESTED,
            ),
            (ProjectStatus.PAUSED_BY_BOSS, ProjectExecutionStopReason.PAUSED),
        ):
            with self.subTest(status=status):
                state = make_state((make_task("task-a"), make_task("task-b")))
                service, store, cycles = build_service(
                    state,
                    cycle_options={"boundary_statuses": {"task-a": status}},
                )
                outcome = service.run()
                self.assertIs(outcome.stop_reason, reason)
                self.assertIs(outcome.final_project_status, status)
                self.assertEqual(outcome.task_ids, ("task-a",))
                self.assertEqual(
                    tuple(item.task.id for item in cycles.requests), ("task-a",)
                )
                self.assertIsNone(store.current.project.current_task_id)
                self.assertIs(store.current.tasks[0].status, TaskStatus.COMPLETED)
                self.assertIs(store.current.tasks[1].status, TaskStatus.PENDING)

    def test_task_limit_is_a_persisted_human_gate(self):
        state = make_state((make_task("task-a"), make_task("task-b")))
        service, store, _ = build_service(state, max_tasks=1)
        outcome = service.run()
        self.assertIs(
            outcome.stop_reason, ProjectExecutionStopReason.TASK_LIMIT_REACHED
        )
        self.assertEqual(outcome.task_ids, ("task-a",))
        self.assertIs(store.current.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(
            store.current.events[-1].event_type,
            "project.execution_limit_reached",
        )

    def test_startup_in_progress_task_requires_recovery_without_execution(self):
        current = make_task("task-a", status=TaskStatus.IN_PROGRESS)
        state = make_state((current,), current_task_id=current.id)
        service, store, cycles = build_service(state)
        outcome = service.run()
        self.assertIs(
            outcome.stop_reason, ProjectExecutionStopReason.HUMAN_REQUIRED
        )
        self.assertEqual(cycles.requests, [])
        self.assertIs(store.current.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(
            store.current.events[-1].event_type,
            "project.execution_recovery_required",
        )

    def test_declared_milestone_and_task_order_is_stable(self):
        tasks = (
            make_task("task-1", milestone_id="milestone-1"),
            make_task("task-2", milestone_id="milestone-1"),
            make_task("task-3", milestone_id="milestone-2"),
        )
        state = make_state(
            tasks,
            milestone_specs=(
                ("milestone-1", ("task-1", "task-2")),
                ("milestone-2", ("task-3",)),
            ),
        )
        outcome = build_service(state)[0].run()
        self.assertEqual(outcome.task_ids, ("task-1", "task-2", "task-3"))

    def test_reopened_task_is_dispatched_as_in_progress(self):
        state = make_state((make_task("task-a", status=TaskStatus.REOPENED),))
        service, store, cycles = build_service(state)
        service.run()
        self.assertIs(cycles.requests[0].task.status, TaskStatus.IN_PROGRESS)
        dispatch = next(
            event for event in store.current.events if event.event_type == "task.dispatched"
        )
        self.assertEqual(dispatch.metadata, {"previous_status": "reopened"})

    def test_dispatch_save_failure_prevents_task_cycle_side_effect(self):
        state = make_state((make_task("task-a"),))
        store = FakeStore(state, fail_on_save=1)
        service, _, cycles = build_service(state, store=store)
        with self.assertRaises(OSError):
            service.run()
        self.assertEqual(cycles.requests, [])
        self.assertIs(store.current.tasks[0].status, TaskStatus.PENDING)

    def test_existing_completed_milestone_does_not_emit_duplicate_event(self):
        state = make_state(
            (
                make_task("task-a", status=TaskStatus.COMPLETED),
                make_task("task-b", milestone_id="milestone-2"),
            ),
            milestone_specs=(
                ("milestone-1", ("task-a",), "completed"),
                ("milestone-2", ("task-b",)),
            ),
        )
        _, store, _ = build_service(state)
        service, store, _ = build_service(state, store=store)
        service.run()
        completed_entities = tuple(
            event.entity_id
            for event in store.current.events
            if event.event_type == "milestone.completed"
        )
        self.assertEqual(completed_entities, ("milestone-2",))


if __name__ == "__main__":
    unittest.main()
