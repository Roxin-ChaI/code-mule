from dataclasses import replace
from datetime import UTC, datetime
import unittest

from code_mule.domain import (
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    ProjectStatus,
    TaskStatus,
)
from code_mule.human import (
    HumanActionNotFound,
    HumanResolutionService,
    InvalidHumanResolution,
    request_human_action,
)
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


def service(store):
    return HumanResolutionService(
        store,
        clock=lambda: NOW,
        event_id_factory=lambda: "human-event",
        resolution_id_factory=lambda: "resolution-1",
    )


class HumanResolutionServiceTests(unittest.TestCase):
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

    def test_uncertain_recovery_cannot_be_converted_to_retry(self):
        store = MemoryStore(gated_state(HumanActionCategory.RECOVERY_UNCERTAIN))
        with self.assertRaisesRegex(InvalidHumanResolution, "not safe"):
            service(store).resolve("action-1", HumanResolutionStrategy.RETRY_TASK)
        self.assertIs(store.state.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_fail_project_is_an_explicit_nonapproval_resolution(self):
        store = MemoryStore(gated_state(HumanActionCategory.SUPERVISOR_FAILURE))
        updated = service(store).resolve(
            "action-1", HumanResolutionStrategy.FAIL_PROJECT
        )
        self.assertIs(updated.project.status, ProjectStatus.FAILED)
        self.assertIs(updated.human_actions[0].status, HumanActionStatus.RESOLVED)


if __name__ == "__main__":
    unittest.main()
