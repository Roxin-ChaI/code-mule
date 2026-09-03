"""Single-owner lease lifecycle and fail-closed crash recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
import os
from pathlib import Path
import threading
from typing import Protocol

from code_mule.domain import HumanActionCategory, ProjectEvent, ProjectStatus, TaskStatus
from code_mule.human import pending_action, request_human_action
from code_mule.state.models import ProjectState

from .contracts import (
    ExecutionAlreadyOwned,
    ExecutionLease,
    ExecutionLeaseStatus,
    ExecutionOwnershipError,
    ExecutionRecoveryRequired,
    RecoveryClassification,
    RecoveryDecision,
)
from .lock import LocalExecutionLock, LocalExecutionLockBusy


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...
    def save(self, state: ProjectState) -> None: ...


class ExecutionOwnershipHandle:
    def __init__(
        self,
        service: "ExecutionOwnershipService",
        lease: ExecutionLease,
        local_lock: LocalExecutionLock,
        heartbeat_interval_seconds: float,
    ) -> None:
        self._service = service
        self._lease = lease
        self._local_lock = local_lock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._closed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="code-mule-execution-heartbeat",
        )
        self._thread.start()

    @property
    def lease(self) -> ExecutionLease:
        return self._lease

    def record_worker_identity(
        self, task_id: str, thread_id: str, attempt: int
    ) -> None:
        self._lease = self._service.record_worker_identity(
            self._lease.id, task_id, thread_id, attempt
        )
        self._local_lock.update(self._lease)

    def clear_worker_identity(self, task_id: str) -> None:
        self._lease = self._service.clear_worker_identity(
            self._lease.id, task_id
        )
        self._local_lock.update(self._lease)

    def heartbeat(self) -> None:
        self._lease = self._service.heartbeat(self._lease.id)
        self._local_lock.update(self._lease)

    def close(self, *, interrupted: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._heartbeat_interval_seconds + 1.0))
        try:
            self._lease = self._service.release(
                self._lease.id, interrupted=interrupted
            )
            self._local_lock.update(self._lease)
        finally:
            self._local_lock.release(self._service.now())

    def __enter__(self) -> "ExecutionOwnershipHandle":
        return self

    def __exit__(self, exception_type, *_: object) -> None:
        self.close(interrupted=exception_type is not None)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_interval_seconds):
            try:
                self._local_lock.heartbeat(self._service.now())
            except BaseException:
                return


class ExecutionOwnershipService:
    def __init__(
        self,
        *,
        store: ProjectStateStore,
        lock_path: Path,
        clock: Callable[[], datetime],
        lease_id_factory: Callable[[], str],
        owner_id_factory: Callable[[], str],
        event_id_factory: Callable[[], str],
        pid_factory: Callable[[], int] = os.getpid,
        heartbeat_interval_seconds: float = 15.0,
        stale_after: timedelta = timedelta(seconds=60),
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if stale_after.total_seconds() <= 0:
            raise ValueError("stale_after must be positive")
        self._store = store
        self._lock_path = lock_path
        self._clock = clock
        self._lease_id_factory = lease_id_factory
        self._owner_id_factory = owner_id_factory
        self._event_id_factory = event_id_factory
        self._pid_factory = pid_factory
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._stale_after = stale_after

    def now(self) -> datetime:
        return self._clock()

    def acquire(self) -> ExecutionOwnershipHandle:
        state = self._store.load()
        operation_time = self.now()
        lease = ExecutionLease(
            id=self._lease_id_factory(),
            project_id=state.project.id,
            owner_id=self._owner_id_factory(),
            pid=self._pid_factory(),
            acquired_at=operation_time,
            heartbeat_at=operation_time,
            status=ExecutionLeaseStatus.ACTIVE,
        )
        local_lock = LocalExecutionLock(self._lock_path)
        try:
            local_lock.acquire(lease)
        except LocalExecutionLockBusy as error:
            active = self._active_lease(self._store.load())
            busy = active or error.lease
            if busy is None:
                raise ExecutionOwnershipError(
                    "local execution lock is held without readable owner metadata"
                ) from error
            raise ExecutionAlreadyOwned(busy) from error

        try:
            state = self._store.load()
            active = self._active_lease(state)
            if active is not None:
                state, decision = self._recover_stale(
                    state, active, operation_time
                )
                if decision.classification in {
                    RecoveryClassification.SESSION_RECOVERY_REQUIRED,
                    RecoveryClassification.SIDE_EFFECT_UNCERTAIN,
                }:
                    self._store.save(state)
                    raise ExecutionRecoveryRequired(decision)
            acquired_event = self._event(
                state,
                "execution.acquired",
                lease.id,
                operation_time,
                {"owner_id": lease.owner_id},
            )
            acquired = replace(
                state,
                execution_leases=state.execution_leases + (lease,),
                events=state.events + (acquired_event,),
            )
            self._store.save(acquired)
        except BaseException:
            local_lock.release(self.now())
            raise
        return ExecutionOwnershipHandle(
            self,
            lease,
            local_lock,
            self._heartbeat_interval_seconds,
        )

    def heartbeat(self, lease_id: str) -> ExecutionLease:
        return self._update_active(
            lease_id,
            lambda lease: replace(lease, heartbeat_at=self.now()),
        )

    def record_worker_identity(
        self, lease_id: str, task_id: str, thread_id: str, attempt: int
    ) -> ExecutionLease:
        return self._update_active(
            lease_id,
            lambda lease: replace(
                lease,
                heartbeat_at=self.now(),
                current_task_id=task_id,
                codex_thread_id=thread_id,
                attempt=attempt,
            ),
        )

    def clear_worker_identity(
        self, lease_id: str, task_id: str
    ) -> ExecutionLease:
        def clear(lease: ExecutionLease) -> ExecutionLease:
            if lease.current_task_id != task_id:
                raise ExecutionOwnershipError(
                    "Worker identity does not belong to the completed Task"
                )
            return replace(
                lease,
                heartbeat_at=self.now(),
                current_task_id=None,
                codex_thread_id=None,
                attempt=None,
            )

        return self._update_active(lease_id, clear)

    def release(self, lease_id: str, *, interrupted: bool) -> ExecutionLease:
        state = self._store.load()
        active = self._lease(state, lease_id)
        if active.status is not ExecutionLeaseStatus.ACTIVE:
            return active
        operation_time = self.now()
        updated_state = state
        if (
            interrupted
            and state.project.status
            in {ProjectStatus.RUNNING, ProjectStatus.CANCEL_REQUESTED}
            and state.project.current_task_id is not None
        ):
            if pending_action(state) is None:
                updated_state = request_human_action(
                    state,
                    category=HumanActionCategory.RECOVERY_UNCERTAIN,
                    summary="Execution was interrupted during an active Task",
                    requested_action="Inspect repository and Task state before resolving",
                    risk="Retrying may duplicate an incomplete Worker side effect",
                    task_id=state.project.current_task_id,
                    operation_time=operation_time,
                    action_id=f"action-{self._event_id_factory()}",
                    event_id_factory=self._event_id_factory,
                    source_event_types=("execution.recovery_required",),
                    source_metadata={"classification": "side_effect_uncertain"},
                )
        released = replace(
            active,
            heartbeat_at=operation_time,
            status=ExecutionLeaseStatus.RELEASED,
        )
        event = self._event(
            updated_state,
            "execution.released",
            released.id,
            operation_time,
            {"interrupted": str(interrupted).lower()},
        )
        saved = replace(
            updated_state,
            execution_leases=self._replace_lease(
                updated_state.execution_leases, released
            ),
            events=updated_state.events + (event,),
        )
        self._store.save(saved)
        return released

    def _recover_stale(
        self,
        state: ProjectState,
        active: ExecutionLease,
        operation_time: datetime,
    ) -> tuple[ProjectState, RecoveryDecision]:
        classification = self._classify(state, active)
        stale = replace(
            active,
            heartbeat_at=operation_time,
            status=ExecutionLeaseStatus.STALE,
        )
        age = operation_time - active.heartbeat_at
        stale_event = self._event(
            state,
            "execution.stale_detected",
            active.id,
            operation_time,
            {
                "classification": classification.value,
                "heartbeat_stale": str(age >= self._stale_after).lower(),
                "owner_pid_alive": str(
                    self._pid_is_alive(active.pid)
                ).lower(),
            },
        )
        updated = replace(
            state,
            execution_leases=self._replace_lease(state.execution_leases, stale),
            events=state.events + (stale_event,),
        )
        decision = RecoveryDecision(
            classification,
            active.id,
            state.project.current_task_id,
        )
        if classification in {
            RecoveryClassification.SESSION_RECOVERY_REQUIRED,
            RecoveryClassification.SIDE_EFFECT_UNCERTAIN,
        }:
            updated = request_human_action(
                updated,
                category=HumanActionCategory.RECOVERY_UNCERTAIN,
                summary="Previous execution ended during an active Task",
                requested_action="Inspect repository and Task state before resolving",
                risk="Starting another Worker may duplicate an uncertain side effect",
                task_id=state.project.current_task_id,
                operation_time=operation_time,
                action_id=f"action-{self._event_id_factory()}",
                event_id_factory=self._event_id_factory,
                source_event_types=("execution.recovery_required",),
                source_metadata={"classification": classification.value},
            )
        else:
            recovered_event = self._event(
                updated,
                "execution.recovered",
                active.id,
                operation_time,
                {"classification": classification.value},
            )
            updated = replace(
                updated, events=updated.events + (recovered_event,)
            )
        return updated, decision

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _classify(
        state: ProjectState, active: ExecutionLease
    ) -> RecoveryClassification:
        if state.project.status not in {
            ProjectStatus.PLANNING,
            ProjectStatus.RUNNING,
            ProjectStatus.REPLANNING,
            ProjectStatus.CANCEL_REQUESTED,
        }:
            return RecoveryClassification.STALE_IDLE_LEASE
        if state.project.current_task_id is None:
            return RecoveryClassification.SAFE_TO_RESUME
        task = next(
            (
                item
                for item in state.tasks
                if item.id == state.project.current_task_id
            ),
            None,
        )
        if task is None or task.status is not TaskStatus.IN_PROGRESS:
            return RecoveryClassification.SIDE_EFFECT_UNCERTAIN
        if active.codex_thread_id is not None:
            return RecoveryClassification.SESSION_RECOVERY_REQUIRED
        return RecoveryClassification.SIDE_EFFECT_UNCERTAIN

    def _update_active(
        self,
        lease_id: str,
        operation: Callable[[ExecutionLease], ExecutionLease],
    ) -> ExecutionLease:
        state = self._store.load()
        active = self._lease(state, lease_id)
        if active.status is not ExecutionLeaseStatus.ACTIVE:
            raise ExecutionOwnershipError("execution lease is not active")
        updated = operation(active)
        self._store.save(
            replace(
                state,
                execution_leases=self._replace_lease(
                    state.execution_leases, updated
                ),
            )
        )
        return updated

    @staticmethod
    def _active_lease(state: ProjectState) -> ExecutionLease | None:
        active = tuple(
            lease
            for lease in state.execution_leases
            if lease.status is ExecutionLeaseStatus.ACTIVE
        )
        if len(active) > 1:
            raise ExecutionOwnershipError(
                "ProjectState contains multiple active execution leases"
            )
        return active[0] if active else None

    @staticmethod
    def _lease(state: ProjectState, lease_id: str) -> ExecutionLease:
        matches = tuple(
            lease for lease in state.execution_leases if lease.id == lease_id
        )
        if len(matches) != 1:
            raise ExecutionOwnershipError(
                "execution lease must exist exactly once"
            )
        return matches[0]

    @staticmethod
    def _replace_lease(
        leases: tuple[ExecutionLease, ...], updated: ExecutionLease
    ) -> tuple[ExecutionLease, ...]:
        return tuple(
            updated if lease.id == updated.id else lease for lease in leases
        )

    def _event(
        self,
        state: ProjectState,
        event_type: str,
        entity_id: str,
        timestamp: datetime,
        metadata: dict[str, str],
    ) -> ProjectEvent:
        return ProjectEvent(
            self._event_id_factory(),
            state.project.id,
            event_type,
            entity_id,
            timestamp,
            metadata,
        )


__all__ = ["ExecutionOwnershipHandle", "ExecutionOwnershipService"]
