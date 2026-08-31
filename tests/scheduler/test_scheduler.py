import unittest
from dataclasses import replace
from datetime import UTC, datetime

from code_mule.domain.enums import PlanStatus, ProjectStatus, TaskStatus
from code_mule.domain.models import Milestone, Plan, Project, Task
from code_mule.scheduler import (
    DependencyCycleDetected,
    NoActivePlan,
    PlanStateInvalid,
    TaskGraphInvalid,
    TaskScheduler,
    UnknownTaskDependency,
    select_next_task,
)
from code_mule.state.models import ProjectState


NOW = datetime(2026, 9, 1, tzinfo=UTC)


def make_task(
    task_id,
    status=TaskStatus.PENDING,
    dependencies=(),
    milestone_id="milestone-1",
):
    return Task(
        id=task_id,
        milestone_id=milestone_id,
        title=f"Task {task_id}",
        description=f"Execute {task_id}",
        status=status,
        dependencies=dependencies,
        acceptance_criteria=(f"{task_id} accepted",),
        execution_attempts=0,
        created_at=NOW,
        updated_at=NOW,
    )


def make_state(
    *,
    tasks=None,
    milestones=None,
    active_plan_id="plan-1",
    plans=None,
):
    if tasks is None:
        tasks = (make_task("task-1"),)
    if milestones is None:
        milestones = (Milestone("milestone-1", "plan-1", "First", "active", tuple(task.id for task in tasks)),)
    if plans is None:
        plans = (
            Plan(
                "plan-1",
                "project-1",
                1,
                PlanStatus.ACTIVE,
                (),
                tuple(milestone.id for milestone in milestones),
                NOW,
            ),
        )
    return ProjectState(
        project=Project(
            "project-1",
            "Scheduler Project",
            ProjectStatus.RUNNING,
            active_plan_id,
            None,
            NOW,
            NOW,
        ),
        requirements=(),
        plans=plans,
        milestones=milestones,
        tasks=tasks,
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


class SchedulerResolutionTests(unittest.TestCase):
    def test_active_plan_must_exist_exactly_once_and_be_active(self):
        scheduler = TaskScheduler()
        with self.assertRaises(NoActivePlan):
            scheduler.validate(make_state(active_plan_id=None))
        with self.assertRaises(PlanStateInvalid):
            scheduler.validate(make_state(active_plan_id="missing"))

        state = make_state()
        duplicate = replace(state, plans=state.plans + state.plans)
        with self.assertRaises(PlanStateInvalid):
            scheduler.validate(duplicate)

        completed = replace(
            state,
            plans=(replace(state.plans[0], status=PlanStatus.COMPLETED),),
        )
        with self.assertRaises(PlanStateInvalid):
            scheduler.validate(completed)

    def test_milestone_and_task_membership_must_be_unambiguous(self):
        state = make_state()
        cases = (
            replace(state, milestones=()),
            replace(state, milestones=state.milestones + state.milestones),
            replace(
                state,
                milestones=(replace(state.milestones[0], plan_id="other"),),
            ),
            replace(state, tasks=()),
            replace(state, tasks=state.tasks + state.tasks),
            replace(
                state,
                tasks=(replace(state.tasks[0], milestone_id="other"),),
            ),
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises((PlanStateInvalid, TaskGraphInvalid)):
                    TaskScheduler().validate(case)

    def test_empty_active_plan_is_invalid_not_complete(self):
        milestone = Milestone("milestone-1", "plan-1", "Empty", "active", ())
        state = make_state(tasks=(), milestones=(milestone,))
        with self.assertRaises(PlanStateInvalid):
            TaskScheduler().is_plan_complete(state)

    def test_historical_plan_tasks_do_not_participate(self):
        current = make_task("current")
        historical = make_task("historical", milestone_id="old-milestone")
        milestone = Milestone("milestone-1", "plan-1", "Current", "active", ("current",))
        old_milestone = Milestone("old-milestone", "old-plan", "Old", "completed", ("historical",))
        old_plan = Plan("old-plan", "project-1", 1, PlanStatus.SUPERSEDED, (), ("old-milestone",), NOW)
        active_plan = Plan(
            "plan-1",
            "project-1",
            1,
            PlanStatus.ACTIVE,
            (),
            ("milestone-1",),
            NOW,
        )
        state = make_state(
            tasks=(historical, current),
            milestones=(milestone, old_milestone),
            plans=(active_plan, old_plan),
        )
        self.assertEqual(TaskScheduler().select_next(state).id, "current")


class SchedulerGraphTests(unittest.TestCase):
    def test_unknown_dependency_fails_before_selection(self):
        state = make_state(tasks=(make_task("task-1", dependencies=("missing",)),))
        with self.assertRaises(UnknownTaskDependency):
            select_next_task(state)

    def test_dependency_cycle_and_self_cycle_are_detected(self):
        cycles = (
            (
                make_task("a", dependencies=("b",)),
                make_task("b", dependencies=("a",)),
            ),
            (make_task("a", dependencies=("a",)),),
        )
        for tasks in cycles:
            with self.subTest(tasks=tasks):
                with self.assertRaises(DependencyCycleDetected):
                    TaskScheduler().validate(make_state(tasks=tasks))

    def test_dependency_requires_completed_not_cancelled(self):
        tasks = (
            make_task("a", TaskStatus.CANCELLED),
            make_task("b", dependencies=("a",)),
        )
        state = make_state(tasks=tasks)
        self.assertIsNone(TaskScheduler().select_next(state))
        self.assertFalse(TaskScheduler().is_plan_complete(state))


class SchedulerSelectionTests(unittest.TestCase):
    def test_declared_milestone_then_task_order_is_stable(self):
        tasks = (
            make_task("t3", milestone_id="milestone-2"),
            make_task("t2"),
            make_task("t1"),
        )
        milestones = (
            Milestone("milestone-1", "plan-1", "First", "active", ("t1", "t2")),
            Milestone("milestone-2", "plan-1", "Second", "active", ("t3",)),
        )
        state = make_state(tasks=tasks, milestones=milestones)
        self.assertEqual(TaskScheduler().select_next(state).id, "t1")
        state = replace(
            state,
            tasks=tuple(
                replace(task, status=TaskStatus.COMPLETED)
                if task.id == "t1"
                else task
                for task in state.tasks
            ),
        )
        self.assertEqual(TaskScheduler().select_next(state).id, "t2")

    def test_ready_requires_pending_or_reopened_and_completed_dependencies(self):
        tasks = (
            make_task("completed", TaskStatus.COMPLETED),
            make_task("blocked", TaskStatus.BLOCKED),
            make_task("cancelled", TaskStatus.CANCELLED),
            make_task("running", TaskStatus.IN_PROGRESS),
            make_task(
                "reopened",
                TaskStatus.REOPENED,
                dependencies=("completed",),
            ),
        )
        state = make_state(tasks=tasks)
        self.assertEqual(TaskScheduler().select_next(state).id, "reopened")

    def test_plan_completion_requires_all_completed_or_cancelled(self):
        scheduler = TaskScheduler()
        complete = make_state(
            tasks=(
                make_task("a", TaskStatus.COMPLETED),
                make_task("b", TaskStatus.CANCELLED),
            )
        )
        self.assertTrue(scheduler.is_plan_complete(complete))
        for status in (
            TaskStatus.PENDING,
            TaskStatus.REOPENED,
            TaskStatus.IN_PROGRESS,
            TaskStatus.BLOCKED,
        ):
            with self.subTest(status=status):
                state = make_state(tasks=(make_task("a", status),))
                self.assertFalse(scheduler.is_plan_complete(state))


if __name__ == "__main__":
    unittest.main()
