import unittest
from dataclasses import replace
from datetime import UTC, datetime

from code_mule.domain import ProjectStatus, TaskStatus
from code_mule.orchestrator import (
    InvalidBossCommand,
    OrchestratorService,
    StopCommand,
)
from code_mule.progress import ProgressEventType, RecordingProgressSink
from tests.state import make_project_state


NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


class Store:
    def __init__(self, state):
        self.state = state
        self.saved = []

    def load(self):
        return self.state

    def save(self, state):
        self.state = state
        self.saved.append(state)


class Ids:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"cancel-event-{self.value}"


def service(state):
    store = Store(state)
    progress = RecordingProgressSink()
    return (
        OrchestratorService(
            store,
            clock=lambda: NOW,
            event_id_factory=Ids(),
            progress_sink=progress,
        ),
        store,
        progress,
    )


class CancellationServiceTests(unittest.TestCase):
    def test_active_task_records_request_without_hard_cancelling_task(self):
        initial = make_project_state()
        orchestrator, store, progress = service(initial)

        result = orchestrator.stop(StopCommand(initial.project.id))

        self.assertIs(result.current_status, ProjectStatus.CANCEL_REQUESTED)
        self.assertTrue(result.safe_point_required)
        self.assertEqual(store.state.project.current_task_id, "task-1")
        self.assertIs(store.state.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(store.state.events[-1].event_type, "project.cancel_requested")
        self.assertEqual(
            store.state.events[-1].metadata,
            {"command": "stop", "reason": "boss_requested"},
        )
        self.assertEqual(
            progress.events[-1].type,
            ProgressEventType.PROJECT_CANCELLATION_REQUESTED,
        )

    def test_boss_reason_is_not_persisted_as_sensitive_free_text(self):
        initial = make_project_state()
        orchestrator, store, _ = service(initial)
        orchestrator.stop(StopCommand(initial.project.id, "sk-secret-value"))
        metadata = store.state.events[-1].metadata
        self.assertNotIn("sk-secret-value", metadata.values())

    def test_safe_point_cancels_unfinished_tasks_and_preserves_completed_history(self):
        initial = make_project_state()
        pending = replace(
            initial.tasks[0],
            id="task-2",
            status=TaskStatus.PENDING,
            dependencies=("task-1",),
        )
        milestone = replace(
            initial.milestones[0], task_ids=("task-1", "task-2")
        )
        initial = replace(initial, tasks=initial.tasks + (pending,), milestones=(milestone,))
        orchestrator, store, _ = service(initial)
        orchestrator.stop(StopCommand(initial.project.id))
        at_safe_point = replace(
            store.state,
            project=replace(store.state.project, current_task_id=None),
            tasks=(replace(store.state.tasks[0], status=TaskStatus.COMPLETED), pending),
        )
        store.state = at_safe_point

        result = orchestrator.complete_cancellation()

        self.assertIs(result.current_status, ProjectStatus.CANCELLED)
        self.assertEqual(
            tuple(task.status for task in store.state.tasks),
            (TaskStatus.COMPLETED, TaskStatus.CANCELLED),
        )
        self.assertIsNone(store.state.project.current_task_id)
        self.assertEqual(store.state.events[-1].event_type, "project.cancelled")
        self.assertEqual(store.state.events[-1].metadata, {"rollback": "not_performed"})

    def test_idle_control_states_cancel_immediately(self):
        for status in (
            ProjectStatus.RUNNING,
            ProjectStatus.PAUSED_BY_BOSS,
            ProjectStatus.CHANGE_REQUESTED,
            ProjectStatus.REPLANNING,
            ProjectStatus.HUMAN_REQUIRED,
        ):
            with self.subTest(status=status):
                initial = make_project_state()
                initial = replace(
                    initial,
                    project=replace(initial.project, status=status, current_task_id=None),
                    tasks=(replace(initial.tasks[0], status=TaskStatus.PENDING),),
                )
                orchestrator, store, _ = service(initial)
                result = orchestrator.stop(StopCommand(initial.project.id))
                self.assertIs(result.current_status, ProjectStatus.CANCELLED)
                self.assertIs(store.state.tasks[0].status, TaskStatus.CANCELLED)

    def test_human_required_with_stale_current_task_cancels_immediately(self):
        initial = make_project_state()
        initial = replace(
            initial,
            project=replace(initial.project, status=ProjectStatus.HUMAN_REQUIRED),
        )
        orchestrator, store, _ = service(initial)
        result = orchestrator.stop(StopCommand(initial.project.id))
        self.assertFalse(result.safe_point_required)
        self.assertIs(store.state.project.status, ProjectStatus.CANCELLED)
        self.assertIs(store.state.tasks[0].status, TaskStatus.CANCELLED)

    def test_done_rejects_stop_and_cancelled_stop_is_idempotent(self):
        done = make_project_state()
        done = replace(done, project=replace(done.project, status=ProjectStatus.DONE))
        orchestrator, store, _ = service(done)
        with self.assertRaises(InvalidBossCommand):
            orchestrator.stop(StopCommand(done.project.id))
        self.assertEqual(store.saved, [])

        cancelled = replace(
            done, project=replace(done.project, status=ProjectStatus.CANCELLED)
        )
        orchestrator, store, _ = service(cancelled)
        result = orchestrator.stop(StopCommand(cancelled.project.id))
        self.assertFalse(result.state_changed)
        self.assertEqual(store.saved, [])


if __name__ == "__main__":
    unittest.main()
