"""Standard-library live console renderer for progress telemetry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
import sys
import threading
import time
from typing import TextIO

from .contracts import ProgressEvent, ProgressEventType, ProgressSnapshot


_SPINNER = ("|", "/", "-", "\\")
_RECENT_LIMIT = 5


class ConsoleProgressRenderer:
    """Render a live TTY dashboard or durable line-oriented progress log."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        refresh_interval: float = 0.15,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if refresh_interval < 0.05:
            raise ValueError("refresh_interval must be at least 0.05 seconds")
        self._stream = stream or sys.stdout
        self._refresh_interval = refresh_interval
        self._monotonic = monotonic
        self._tty = bool(self._stream.isatty())
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._frame = 0
        self._last_line_count = 0
        self._project_started_mono: float | None = None
        self._stage_started_mono: float | None = None
        self._snapshot = ProgressSnapshot(
            project_id=None,
            project_status=None,
            completed_tasks=0,
            total_tasks=0,
            current_task_id=None,
            current_task_title=None,
            current_attempt=None,
            worker_status="Waiting",
            supervisor_status="Waiting",
            project_started_at=None,
            task_started_at=None,
            stage_started_at=None,
            recent_events=(),
        )

    @property
    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def thread_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> ConsoleProgressRenderer:
        if self._started:
            return self
        self._started = True
        if self._tty:
            self._stream.write("\x1b[?25l")
            self._stream.flush()
            self._thread = threading.Thread(
                target=self._render_loop,
                name="code-mule-progress-renderer",
                daemon=True,
            )
            self._thread.start()
        return self

    def emit(self, event: ProgressEvent) -> None:
        with self._lock:
            self._apply(event)
            if not self._tty:
                task = f" {event.task_id}" if event.task_id else ""
                message = f" — {event.message}" if event.message else ""
                self._stream.write(
                    f"[{event.timestamp:%H:%M:%S}] {event.type.value}{task}{message}\n"
                )
                self._stream.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self._refresh_interval * 4))
        if self._tty:
            with self._lock:
                self._draw_locked(final=True)
            self._stream.write("\x1b[?25h")
            self._stream.flush()
        self._thread = None

    def __enter__(self) -> ConsoleProgressRenderer:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()

    def _render_loop(self) -> None:
        while not self._stop.wait(self._refresh_interval):
            with self._lock:
                self._draw_locked(final=False)

    def _apply(self, event: ProgressEvent) -> None:
        snapshot = self._snapshot
        recent = (snapshot.recent_events + (event,))[-_RECENT_LIMIT:]
        updates: dict[str, object] = {"recent_events": recent}
        if event.project_id is not None:
            updates["project_id"] = event.project_id

        if event.type is ProgressEventType.PROJECT_STARTED:
            updates.update(
                project_status=event.metadata.get("project_status", "running"),
                completed_tasks=_count(event, "completed_tasks", 0),
                total_tasks=_count(event, "total_tasks", 0),
                project_started_at=event.timestamp,
            )
            self._project_started_mono = self._monotonic()
        elif event.type is ProgressEventType.PLANNING_STARTED:
            updates.update(
                project_status="planning",
                completed_tasks=0,
                total_tasks=0,
                project_started_at=event.timestamp,
                worker_status="Waiting",
                supervisor_status="Waiting",
            )
            self._project_started_mono = self._monotonic()
        elif event.type is ProgressEventType.SUPERVISOR_PLAN_STARTED:
            updates.update(
                supervisor_status="Planning",
                stage_started_at=event.timestamp,
            )
            self._stage_started_mono = self._monotonic()
        elif event.type is ProgressEventType.SUPERVISOR_PLAN_COMPLETED:
            updates.update(
                supervisor_status="Plan ready",
                stage_started_at=event.timestamp,
            )
            self._stage_started_mono = None
        elif event.type is ProgressEventType.PLANNING_MATERIALIZING:
            updates.update(
                supervisor_status="Completed",
                stage_started_at=event.timestamp,
            )
        elif event.type is ProgressEventType.PLANNING_COMPLETED:
            updates.update(
                project_status="running",
                completed_tasks=0,
                total_tasks=_count(event, "total_tasks", 0),
                supervisor_status="Completed",
            )
            self._stage_started_mono = None
        elif event.type is ProgressEventType.PLANNING_FAILED:
            updates.update(
                project_status="human_required",
                supervisor_status="FAILED",
                stage_started_at=event.timestamp,
            )
            self._stage_started_mono = None
        elif event.type is ProgressEventType.TASK_DISPATCHED:
            updates.update(
                current_task_id=event.task_id,
                current_task_title=event.metadata.get("task_title"),
                completed_tasks=_count(
                    event, "completed_tasks", snapshot.completed_tasks
                ),
                total_tasks=_count(event, "total_tasks", snapshot.total_tasks),
                current_attempt=None,
                worker_status="Waiting",
                supervisor_status="Waiting",
            )
        elif event.type is ProgressEventType.TASK_STARTED:
            updates.update(
                current_task_id=event.task_id,
                current_attempt=event.attempt,
                task_started_at=event.timestamp,
            )
        elif event.type in {
            ProgressEventType.WORKER_STARTING,
            ProgressEventType.WORKER_STARTED,
            ProgressEventType.WORKER_ACTIVITY,
        }:
            updates.update(
                worker_status="Working",
                stage_started_at=event.timestamp,
            )
            if event.task_id is not None:
                updates["current_task_id"] = event.task_id
            if snapshot.worker_status != "Working":
                self._stage_started_mono = self._monotonic()
        elif event.type is ProgressEventType.WORKER_COMPLETED:
            updates.update(worker_status="Completed", stage_started_at=event.timestamp)
            self._stage_started_mono = None
        elif event.type is ProgressEventType.WORKER_FAILED:
            updates.update(worker_status="FAILED", stage_started_at=event.timestamp)
            self._stage_started_mono = None
        elif event.type is ProgressEventType.SUPERVISOR_REVIEW_STARTED:
            updates.update(
                supervisor_status="Reviewing",
                stage_started_at=event.timestamp,
            )
            self._stage_started_mono = self._monotonic()
        elif event.type is ProgressEventType.SUPERVISOR_REVIEW_COMPLETED:
            decision = event.metadata.get("decision", "completed").upper()
            updates.update(supervisor_status=decision, stage_started_at=event.timestamp)
            self._stage_started_mono = None
        elif event.type is ProgressEventType.SUPERVISOR_FAILED:
            updates.update(supervisor_status="FAILED", stage_started_at=event.timestamp)
            self._stage_started_mono = None
        elif event.type is ProgressEventType.TASK_COMPLETED:
            updates.update(
                completed_tasks=_count(
                    event, "completed_tasks", snapshot.completed_tasks
                ),
                total_tasks=_count(event, "total_tasks", snapshot.total_tasks),
            )
        elif event.type is ProgressEventType.PROJECT_COMPLETED:
            updates.update(
                project_status="done",
                completed_tasks=_count(
                    event, "completed_tasks", snapshot.completed_tasks
                ),
                total_tasks=_count(event, "total_tasks", snapshot.total_tasks),
                worker_status="Completed",
                supervisor_status="Completed",
            )
        elif event.type is ProgressEventType.PROJECT_STOPPED:
            updates["project_status"] = event.metadata.get(
                "project_status", "stopped"
            )
        elif event.type in {
            ProgressEventType.HUMAN_GATE,
            ProgressEventType.TASK_HUMAN_REQUIRED,
        }:
            updates["project_status"] = "human_required"
        self._snapshot = replace(snapshot, **updates)

    def _draw_locked(self, *, final: bool) -> None:
        lines = self._dashboard_lines(final=final)
        if self._last_line_count:
            self._stream.write(f"\x1b[{self._last_line_count}F")
        for line in lines:
            self._stream.write(f"\x1b[2K{line}\n")
        self._stream.flush()
        self._last_line_count = len(lines)
        self._frame += 1

    def _dashboard_lines(self, *, final: bool) -> list[str]:
        snapshot = self._snapshot
        spinner = _SPINNER[self._frame % len(_SPINNER)]
        width = 20
        filled = int(snapshot.percentage / 100 * width)
        bar = "#" * filled + "-" * (width - filled)
        project_elapsed = _elapsed(
            self._monotonic(), self._project_started_mono
        )
        stage_elapsed = _elapsed(self._monotonic(), self._stage_started_mono)
        worker = snapshot.worker_status
        supervisor = snapshot.supervisor_status
        if not final and worker == "Working":
            worker = f"{spinner} Working... {stage_elapsed}"
        if not final and supervisor in {"Reviewing", "Planning"}:
            supervisor = f"{spinner} {supervisor}... {stage_elapsed}"
        task = snapshot.current_task_title or snapshot.current_task_id or "—"
        attempt = str(snapshot.current_attempt or "—")
        progress = (
            "Planning"
            if snapshot.project_status == "planning" and snapshot.total_tasks == 0
            else (
                f"[{bar}] {snapshot.completed_tasks} / "
                f"{snapshot.total_tasks} ({snapshot.percentage:.0f}%)"
            )
        )
        lines = [
            "Code Mule",
            "Powered by prompts. Paid in tokens.",
            "",
            f"Project      {snapshot.project_id or '—'}",
            f"Status       {(snapshot.project_status or 'waiting').upper()}",
            f"Progress     {progress}",
            "",
            f"Current Task {task}",
            f"Attempt      {attempt}",
            "",
            f"Codex        {worker}",
            f"Supervisor   {supervisor}",
            f"Elapsed      {project_elapsed}",
            "",
            "Recent Activity",
        ]
        lines.extend(
            f"{event.timestamp:%H:%M:%S} {event.message or event.type.value}"
            for event in snapshot.recent_events
        )
        if snapshot.project_status == "human_required":
            lines.append("PROJECT PAUSED — HUMAN ACTION REQUIRED")
        return lines


def _count(event: ProgressEvent, field: str, fallback: int) -> int:
    value = event.metadata.get(field)
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except ValueError:
        return fallback
    return parsed if parsed >= 0 else fallback


def _elapsed(now: float, started: float | None) -> str:
    seconds = 0 if started is None else max(0, int(now - started))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


__all__ = ["ConsoleProgressRenderer"]
