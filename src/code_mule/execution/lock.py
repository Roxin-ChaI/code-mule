"""Advisory OS file lock for one local execution owner."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path

from .contracts import ExecutionLease, ExecutionLeaseStatus


class LocalExecutionLockBusy(RuntimeError):
    def __init__(self, lease: ExecutionLease | None) -> None:
        super().__init__("local execution lock is already held")
        self.lease = lease


class LocalExecutionLock:
    """Hold an advisory lock for the complete execution session."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None
        self._lease: ExecutionLease | None = None

    def acquire(self, lease: ExecutionLease) -> None:
        if self._descriptor is not None:
            raise RuntimeError("execution lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            busy_lease = self._read_descriptor(descriptor)
            os.close(descriptor)
            raise LocalExecutionLockBusy(busy_lease) from error
        self._descriptor = descriptor
        self._lease = lease
        try:
            self._write(lease)
        except BaseException:
            self._unlock()
            raise

    def update(self, lease: ExecutionLease) -> None:
        if self._descriptor is None:
            raise RuntimeError("execution lock is not acquired")
        self._lease = lease
        self._write(lease)

    def heartbeat(self, timestamp: datetime) -> None:
        if self._lease is None:
            return
        self.update(replace(self._lease, heartbeat_at=timestamp))

    def release(self, timestamp: datetime) -> None:
        if self._descriptor is None:
            return
        try:
            if self._lease is not None:
                released = replace(
                    self._lease,
                    heartbeat_at=timestamp,
                    status=ExecutionLeaseStatus.RELEASED,
                )
                self._write(released)
        finally:
            self._unlock()

    def _write(self, lease: ExecutionLease) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            raise RuntimeError("execution lock is not acquired")
        payload = json.dumps(
            {
                "id": lease.id,
                "project_id": lease.project_id,
                "owner_id": lease.owner_id,
                "pid": lease.pid,
                "acquired_at": lease.acquired_at.isoformat(),
                "heartbeat_at": lease.heartbeat_at.isoformat(),
                "status": lease.status.value,
                "current_task_id": lease.current_task_id,
                "codex_thread_id": lease.codex_thread_id,
                "attempt": lease.attempt,
            },
            sort_keys=True,
        ).encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload)
        os.fsync(descriptor)

    @staticmethod
    def _read_descriptor(descriptor: int) -> ExecutionLease | None:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            raw = os.read(descriptor, 16384)
            payload = json.loads(raw.decode("utf-8"))
            attempt = payload.get("attempt")
            return ExecutionLease(
                id=payload["id"],
                project_id=payload["project_id"],
                owner_id=payload["owner_id"],
                pid=payload["pid"],
                acquired_at=datetime.fromisoformat(payload["acquired_at"]),
                heartbeat_at=datetime.fromisoformat(payload["heartbeat_at"]),
                status=ExecutionLeaseStatus(payload["status"]),
                current_task_id=payload.get("current_task_id"),
                codex_thread_id=payload.get("codex_thread_id"),
                attempt=attempt,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError):
            return None

    def _unlock(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        self._lease = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = ["LocalExecutionLock", "LocalExecutionLockBusy"]
