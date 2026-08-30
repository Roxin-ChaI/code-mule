import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

from code_mule.domain.enums import (
    ChangeRequestStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import ChangeRequest
from code_mule.orchestrator.commands import (
    ChangeCommand,
    PauseCommand,
    QueryCommand,
    ResumeCommand,
)
from code_mule.orchestrator.service import (
    DuplicateChangeRequest,
    InvalidBossCommand,
    OrchestratorService,
    ProjectIdentityMismatch,
)
from code_mule.state.models import ProjectState

from state import CREATED, make_project_state


OPERATION_TIME = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)


class InMemoryProjectStateStore:
    def __init__(self, state: ProjectState, *, save_error: Exception | None = None):
        self.state = state
        self.saved_states: list[ProjectState] = []
        self.save_error = save_error
        self.load_calls = 0

    def load(self) -> ProjectState:
        self.load_calls += 1
        return self.state

    def save(self, state: ProjectState) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved_states.append(state)


def state_with_status(status: ProjectStatus) -> ProjectState:
    state = make_project_state()
    return replace(state, project=replace(state.project, status=status))


def make_service(state: ProjectState, *, save_error: Exception | None = None):
    store = InMemoryProjectStateStore(state, save_error=save_error)
    clock = Mock(return_value=OPERATION_TIME)
    event_ids = Mock(return_value="event-new")
    service = OrchestratorService(
        store,
        clock=clock,
        event_id_factory=event_ids,
    )
    return service, store, clock, event_ids


class QueryTests(unittest.TestCase):
    def test_running_query_computes_counts_and_is_strictly_read_only(self):
        state = make_project_state()
        base_task = state.tasks[0]
        tasks = tuple(
            replace(base_task, id=f"task-{index}", status=status)
            for index, status in enumerate(
                (
                    TaskStatus.PENDING,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.COMPLETED,
                    TaskStatus.BLOCKED,
                    TaskStatus.CANCELLED,
                    TaskStatus.REOPENED,
                ),
                start=1,
            )
        )
        changes = (
            ChangeRequest(
                "change-pending",
                state.project.id,
                "pending",
                ChangeRequestStatus.PENDING,
                (),
                "boss",
                CREATED,
            ),
            ChangeRequest(
                "change-analyzing",
                state.project.id,
                "analyzing",
                ChangeRequestStatus.ANALYZING,
                (),
                "boss",
                CREATED,
            ),
            ChangeRequest(
                "change-applied",
                state.project.id,
                "applied",
                ChangeRequestStatus.APPLIED,
                (),
                "boss",
                CREATED,
            ),
            ChangeRequest(
                "change-rejected",
                state.project.id,
                "rejected",
                ChangeRequestStatus.REJECTED,
                (),
                "boss",
                CREATED,
            ),
        )
        state = replace(state, tasks=tasks, change_requests=changes)
        original_events = state.events
        original_updated_at = state.project.updated_at
        service, store, clock, event_ids = make_service(state)

        view = service.query(QueryCommand(state.project.id))

        self.assertEqual(view.status, ProjectStatus.RUNNING)
        self.assertEqual(view.total_tasks, 6)
        self.assertEqual(view.completed_tasks, 1)
        self.assertEqual(view.in_progress_tasks, 1)
        self.assertEqual(view.pending_tasks, 1)
        self.assertEqual(view.blocked_tasks, 1)
        self.assertEqual(view.open_change_requests, 2)
        self.assertIs(view.quality_status, state.quality_status)
        self.assertEqual(store.saved_states, [])
        self.assertIs(state.events, original_events)
        self.assertEqual(state.project.updated_at, original_updated_at)
        clock.assert_not_called()
        event_ids.assert_not_called()

    def test_query_is_allowed_for_paused_and_done_projects(self):
        for status in (ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.DONE):
            with self.subTest(status=status):
                state = state_with_status(status)
                service, store, clock, event_ids = make_service(state)
                view = service.query(QueryCommand(state.project.id))
                self.assertEqual(view.status, status)
                self.assertEqual(store.saved_states, [])
                clock.assert_not_called()
                event_ids.assert_not_called()

    def test_query_identity_mismatch_does_not_save(self):
        service, store, clock, event_ids = make_service(make_project_state())
        with self.assertRaises(ProjectIdentityMismatch):
            service.query(QueryCommand("other-project"))
        self.assertEqual(store.saved_states, [])
        clock.assert_not_called()
        event_ids.assert_not_called()


class PauseTests(unittest.TestCase):
    def test_pause_creates_copy_on_write_state_and_audit_event(self):
        original = make_project_state()
        original_events = original.events
        service, store, clock, event_ids = make_service(original)

        result = service.pause(PauseCommand(original.project.id))

        self.assertEqual(len(store.saved_states), 1)
        saved = store.saved_states[0]
        self.assertIsNot(saved, original)
        self.assertIsNot(saved.project, original.project)
        self.assertEqual(original.project.status, ProjectStatus.RUNNING)
        self.assertIs(original.events, original_events)
        self.assertEqual(saved.project.status, ProjectStatus.PAUSED_BY_BOSS)
        self.assertEqual(saved.project.updated_at, OPERATION_TIME)
        self.assertEqual(saved.events[:-1], original.events)
        event = saved.events[-1]
        self.assertEqual(event.id, "event-new")
        self.assertEqual(event.event_type, "boss.pause")
        self.assertEqual(event.entity_id, original.project.id)
        self.assertEqual(event.metadata, {"command": "pause"})
        self.assertEqual(event.timestamp, OPERATION_TIME)
        self.assertEqual(result.previous_status, ProjectStatus.RUNNING)
        self.assertEqual(result.current_status, ProjectStatus.PAUSED_BY_BOSS)
        self.assertTrue(result.state_changed)
        clock.assert_called_once_with()
        event_ids.assert_called_once_with()

    def test_pause_rejects_paused_and_idle_without_side_effects(self):
        for status in (ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.IDLE):
            with self.subTest(status=status):
                service, store, clock, event_ids = make_service(
                    state_with_status(status)
                )
                with self.assertRaises(InvalidBossCommand):
                    service.pause(PauseCommand("project-1"))
                self.assertEqual(store.saved_states, [])
                clock.assert_not_called()
                event_ids.assert_not_called()


class ResumeTests(unittest.TestCase):
    def test_resume_creates_copy_on_write_state_and_audit_event(self):
        original = state_with_status(ProjectStatus.PAUSED_BY_BOSS)
        original_events = original.events
        service, store, clock, event_ids = make_service(original)

        result = service.resume(ResumeCommand(original.project.id))

        self.assertEqual(len(store.saved_states), 1)
        saved = store.saved_states[0]
        self.assertEqual(original.project.status, ProjectStatus.PAUSED_BY_BOSS)
        self.assertIs(original.events, original_events)
        self.assertEqual(saved.project.status, ProjectStatus.RUNNING)
        self.assertEqual(saved.project.updated_at, OPERATION_TIME)
        event = saved.events[-1]
        self.assertEqual(event.event_type, "boss.resume")
        self.assertEqual(event.entity_id, original.project.id)
        self.assertEqual(event.metadata, {"command": "resume"})
        self.assertEqual(event.timestamp, OPERATION_TIME)
        self.assertEqual(result.current_status, ProjectStatus.RUNNING)
        clock.assert_called_once_with()
        event_ids.assert_called_once_with()

    def test_resume_rejects_running_and_done_without_saving(self):
        for status in (ProjectStatus.RUNNING, ProjectStatus.DONE):
            with self.subTest(status=status):
                service, store, clock, event_ids = make_service(
                    state_with_status(status)
                )
                with self.assertRaises(InvalidBossCommand):
                    service.resume(ResumeCommand("project-1"))
                self.assertEqual(store.saved_states, [])
                clock.assert_not_called()
                event_ids.assert_not_called()


class ChangeTests(unittest.TestCase):
    def test_change_from_running_and_paused_creates_request_and_event(self):
        for source in (ProjectStatus.RUNNING, ProjectStatus.PAUSED_BY_BOSS):
            with self.subTest(source=source):
                original = state_with_status(source)
                original_changes = original.change_requests
                original_events = original.events
                service, store, clock, event_ids = make_service(original)
                command = ChangeCommand(
                    original.project.id,
                    "Add a deterministic command",
                    "boss-1",
                    "change-new",
                )

                result = service.change(command)

                self.assertEqual(len(store.saved_states), 1)
                saved = store.saved_states[0]
                self.assertEqual(original.project.status, source)
                self.assertIs(original.change_requests, original_changes)
                self.assertIs(original.events, original_events)
                self.assertEqual(saved.project.status, ProjectStatus.CHANGE_REQUESTED)
                self.assertEqual(saved.project.updated_at, OPERATION_TIME)
                request = saved.change_requests[-1]
                self.assertEqual(request.id, "change-new")
                self.assertEqual(request.description, command.description)
                self.assertEqual(request.created_by, "boss-1")
                self.assertEqual(request.status, ChangeRequestStatus.PENDING)
                self.assertEqual(request.affected_requirement_ids, ())
                self.assertEqual(request.created_at, OPERATION_TIME)
                event = saved.events[-1]
                self.assertEqual(event.id, "event-new")
                self.assertEqual(event.event_type, "boss.change")
                self.assertEqual(event.entity_id, "change-new")
                self.assertEqual(event.metadata, {"command": "change"})
                self.assertEqual(event.timestamp, OPERATION_TIME)
                self.assertEqual(result.change_request_id, "change-new")
                self.assertEqual(result.previous_status, source)
                self.assertEqual(
                    result.current_status, ProjectStatus.CHANGE_REQUESTED
                )
                clock.assert_called_once_with()
                event_ids.assert_called_once_with()

    def test_duplicate_change_request_is_rejected_without_saving(self):
        original = make_project_state()
        duplicate_id = original.change_requests[0].id
        service, store, clock, event_ids = make_service(original)

        with self.assertRaises(DuplicateChangeRequest):
            service.change(
                ChangeCommand("project-1", "duplicate", "boss", duplicate_id)
            )

        self.assertEqual(store.saved_states, [])
        clock.assert_not_called()
        event_ids.assert_not_called()

    def test_change_rejects_invalid_source_without_saving(self):
        for status in (ProjectStatus.IDLE, ProjectStatus.CHANGE_REQUESTED):
            with self.subTest(status=status):
                service, store, clock, event_ids = make_service(
                    state_with_status(status)
                )
                with self.assertRaises(InvalidBossCommand):
                    service.change(
                        ChangeCommand("project-1", "change", "boss", "change-new")
                    )
                self.assertEqual(store.saved_states, [])
                clock.assert_not_called()
                event_ids.assert_not_called()


class IdentityAndPersistenceFailureTests(unittest.TestCase):
    def test_every_command_rejects_project_identity_mismatch_without_saving(self):
        operations = (
            lambda service: service.query(QueryCommand("other-project")),
            lambda service: service.pause(PauseCommand("other-project")),
            lambda service: service.resume(ResumeCommand("other-project")),
            lambda service: service.change(
                ChangeCommand("other-project", "change", "boss", "change-new")
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                service, store, clock, event_ids = make_service(
                    state_with_status(ProjectStatus.PAUSED_BY_BOSS)
                )
                with self.assertRaises(ProjectIdentityMismatch):
                    operation(service)
                self.assertEqual(store.saved_states, [])
                clock.assert_not_called()
                event_ids.assert_not_called()

    def test_persistence_failure_propagates_without_mutating_original(self):
        original = make_project_state()
        service, store, clock, event_ids = make_service(
            original,
            save_error=OSError("save failed"),
        )

        with self.assertRaisesRegex(OSError, "save failed"):
            service.pause(PauseCommand(original.project.id))

        self.assertEqual(original.project.status, ProjectStatus.RUNNING)
        self.assertEqual(len(original.events), 1)
        self.assertEqual(store.saved_states, [])
        clock.assert_called_once_with()
        event_ids.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
