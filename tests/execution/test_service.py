from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.domain import ProjectStatus, TaskStatus
from code_mule.execution import (
    ExecutionAlreadyOwned,
    ExecutionLease,
    ExecutionLeaseStatus,
    ExecutionRecoveryRequired,
    RecoveryClassification,
)
from code_mule.execution.service import ExecutionOwnershipService
from code_mule.state.store import JsonProjectStateStore
from state import make_project_state


NOW = datetime(2026, 9, 3, tzinfo=UTC)


class ExecutionOwnershipServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = JsonProjectStateStore(self.root / "state.json")
        state = make_project_state()
        pending = replace(state.tasks[0], status=TaskStatus.PENDING)
        self.store.save(
            replace(
                state,
                project=replace(state.project, current_task_id=None),
                tasks=(pending,),
            )
        )
        self.ids = iter(f"id-{index}" for index in range(100))
        self.time = NOW

    def tearDown(self):
        self.temporary.cleanup()

    def service(self, *, pid=1234):
        return ExecutionOwnershipService(
            store=self.store,
            lock_path=self.root / "execution.lock",
            clock=lambda: self.time,
            lease_id_factory=lambda: next(self.ids),
            owner_id_factory=lambda: next(self.ids),
            event_id_factory=lambda: next(self.ids),
            pid_factory=lambda: pid,
            heartbeat_interval_seconds=3600,
            stale_after=timedelta(seconds=30),
        )

    def stale_lease(self, *, thread_id=None, pid=9999):
        state = self.store.load()
        return ExecutionLease(
            "stale-lease",
            state.project.id,
            "old-owner",
            pid,
            NOW - timedelta(minutes=5),
            NOW - timedelta(minutes=5),
            ExecutionLeaseStatus.ACTIVE,
            state.project.current_task_id,
            thread_id,
            1 if state.project.current_task_id else None,
        )

    def test_first_owner_acquires_and_second_owner_is_blocked(self):
        first = self.service().acquire()
        active = self.store.load().execution_leases[-1]
        self.assertIs(active.status, ExecutionLeaseStatus.ACTIVE)
        with self.assertRaises(ExecutionAlreadyOwned):
            self.service(pid=5678).acquire()
        self.assertEqual(len(self.store.load().execution_leases), 1)
        first.close()

    def test_acquisition_save_failure_releases_lock_before_any_owner_exists(self):
        class FailOnceStore:
            def __init__(inner_self, delegate):
                inner_self.delegate = delegate
                inner_self.failed = False

            def load(inner_self):
                return inner_self.delegate.load()

            def save(inner_self, state):
                if not inner_self.failed:
                    inner_self.failed = True
                    raise OSError("lease persistence failed")
                inner_self.delegate.save(state)

        failing = FailOnceStore(self.store)
        service = ExecutionOwnershipService(
            store=failing,
            lock_path=self.root / "execution.lock",
            clock=lambda: self.time,
            lease_id_factory=lambda: next(self.ids),
            owner_id_factory=lambda: next(self.ids),
            event_id_factory=lambda: next(self.ids),
            pid_factory=lambda: 1234,
            heartbeat_interval_seconds=3600,
        )
        with self.assertRaisesRegex(OSError, "lease persistence failed"):
            service.acquire()

        handle = self.service(pid=5678).acquire()
        self.assertEqual(len(self.store.load().execution_leases), 1)
        handle.close()

    def test_clean_release_and_lock_metadata(self):
        handle = self.service().acquire()
        handle.close()
        released = self.store.load().execution_leases[-1]
        self.assertIs(released.status, ExecutionLeaseStatus.RELEASED)
        payload = json.loads((self.root / "execution.lock").read_text())
        self.assertEqual(payload["status"], "released")
        self.assertIn("execution.released", self.event_types())

    def test_worker_identity_is_persisted_and_cleared(self):
        handle = self.service().acquire()
        handle.record_worker_identity("task-1", "thread-1", 2)
        active = self.store.load().execution_leases[-1]
        self.assertEqual(
            (active.current_task_id, active.codex_thread_id, active.attempt),
            ("task-1", "thread-1", 2),
        )
        handle.clear_worker_identity("task-1")
        active = self.store.load().execution_leases[-1]
        self.assertEqual(
            (active.current_task_id, active.codex_thread_id, active.attempt),
            (None, None, None),
        )
        handle.close()

    def test_task_boundary_stale_lease_is_safely_recovered_despite_pid_reuse(self):
        state = self.store.load()
        self.store.save(
            replace(
                state,
                execution_leases=(self.stale_lease(pid=os.getpid()),),
            )
        )
        handle = self.service(pid=os.getpid()).acquire()
        recovered = self.store.load()
        self.assertIs(recovered.execution_leases[0].status, ExecutionLeaseStatus.STALE)
        self.assertIs(recovered.execution_leases[1].status, ExecutionLeaseStatus.ACTIVE)
        self.assertIn("execution.recovered", self.event_types())
        stale_event = next(
            event
            for event in recovered.events
            if event.event_type == "execution.stale_detected"
        )
        self.assertEqual(stale_event.metadata["owner_pid_alive"], "true")
        handle.close()

    def test_stale_session_requires_human_and_never_acquires_new_owner(self):
        state = self.store.load()
        running_task = replace(state.tasks[0], status=TaskStatus.IN_PROGRESS)
        state = replace(
            state,
            project=replace(state.project, current_task_id=running_task.id),
            tasks=(running_task,),
        )
        self.store.save(
            replace(
                state,
                execution_leases=(
                    ExecutionLease(
                        "stale-lease", state.project.id, "old-owner", 9999,
                        NOW - timedelta(minutes=5), NOW - timedelta(minutes=5),
                        ExecutionLeaseStatus.ACTIVE, running_task.id, "thread-1", 1,
                    ),
                ),
            )
        )
        with self.assertRaises(ExecutionRecoveryRequired) as raised:
            self.service().acquire()
        self.assertIs(
            raised.exception.decision.classification,
            RecoveryClassification.SESSION_RECOVERY_REQUIRED,
        )
        final = self.store.load()
        self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(len(final.execution_leases), 1)
        self.assertIs(final.execution_leases[0].status, ExecutionLeaseStatus.STALE)
        self.assertEqual(final.human_actions[-1].category.value, "recovery_uncertain")

    def test_interrupted_active_task_creates_recovery_action_and_releases(self):
        handle = self.service().acquire()
        state = self.store.load()
        running_task = replace(state.tasks[0], status=TaskStatus.IN_PROGRESS)
        self.store.save(
            replace(
                state,
                project=replace(state.project, current_task_id=running_task.id),
                tasks=(running_task,),
            )
        )
        handle.record_worker_identity("task-1", "thread-1", 1)
        handle.close(interrupted=True)
        final = self.store.load()
        self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertIs(final.execution_leases[-1].status, ExecutionLeaseStatus.RELEASED)
        self.assertEqual(final.human_actions[-1].category.value, "recovery_uncertain")

    def test_interrupted_cancel_requested_task_stays_fail_closed(self):
        handle = self.service().acquire()
        state = self.store.load()
        running_task = replace(state.tasks[0], status=TaskStatus.IN_PROGRESS)
        self.store.save(
            replace(
                state,
                project=replace(
                    state.project,
                    status=ProjectStatus.CANCEL_REQUESTED,
                    current_task_id=running_task.id,
                ),
                tasks=(running_task,),
            )
        )
        handle.record_worker_identity("task-1", "thread-1", 1)
        handle.close(interrupted=True)
        final = self.store.load()
        self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(final.human_actions[-1].category.value, "recovery_uncertain")

    def test_interrupted_control_state_with_active_task_requires_recovery(self):
        base = self.store.load()
        for status in (
            ProjectStatus.CHANGE_REQUESTED,
            ProjectStatus.PAUSED_BY_BOSS,
        ):
            with self.subTest(status=status):
                self.store.save(replace(base, execution_leases=()))
                handle = self.service().acquire()
                state = self.store.load()
                running_task = replace(state.tasks[0], status=TaskStatus.IN_PROGRESS)
                self.store.save(
                    replace(
                        state,
                        project=replace(
                            state.project,
                            status=status,
                            current_task_id=running_task.id,
                        ),
                        tasks=(running_task,),
                    )
                )
                handle.record_worker_identity("task-1", "thread-1", 1)

                handle.close(interrupted=True)

                final = self.store.load()
                self.assertIs(final.project.status, ProjectStatus.HUMAN_REQUIRED)
                self.assertIs(
                    final.execution_leases[-1].status,
                    ExecutionLeaseStatus.RELEASED,
                )
                self.assertEqual(
                    final.human_actions[-1].category.value,
                    "recovery_uncertain",
                )

    def event_types(self):
        return tuple(event.event_type for event in self.store.load().events)


if __name__ == "__main__":
    unittest.main()
