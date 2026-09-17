"""Pure helpers for replacing the latest recovery evidence atomically."""

from dataclasses import replace
from datetime import datetime

from code_mule.state.models import ProjectState
from code_mule.transport import TransportDiagnostics

from .contracts import (
    BoundaryRecoverability,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionPhase,
    ExecutionStopBoundary,
    ExecutionStopReason,
    SafePoint,
    SafePointKind,
    WorkerTerminalState,
)


def with_safe_point(
    state: ProjectState,
    kind: SafePointKind,
    recorded_at: datetime,
    *,
    task_id: str | None = None,
    attempt: int | None = None,
    head_sha: str | None = None,
) -> ProjectState:
    return replace(
        state,
        latest_safe_point=SafePoint(kind, recorded_at, task_id, attempt, head_sha),
    )


def with_stop_boundary(
    state: ProjectState,
    *,
    reason: ExecutionStopReason,
    phase: ExecutionPhase,
    safe_point: SafePointKind,
    recoverability: BoundaryRecoverability,
    worker_started: bool,
    worker_terminal_state: WorkerTerminalState,
    report_persisted: bool,
    recorded_at: datetime,
    task_id: str | None = None,
    attempt: int | None = None,
    head_sha: str | None = None,
) -> ProjectState:
    return replace(
        state,
        latest_execution_stop=ExecutionStopBoundary(
            reason,
            phase,
            safe_point,
            recoverability,
            worker_started,
            worker_terminal_state,
            report_persisted,
            recorded_at,
            task_id,
            attempt,
            head_sha,
        ),
    )


def start_attempt(
    state: ProjectState,
    *,
    task_id: str,
    attempt: int,
    recorded_at: datetime,
    baseline_head: str | None = None,
    transport: TransportDiagnostics | None = None,
) -> ProjectState:
    if any(item.task_id == task_id and item.attempt == attempt for item in state.execution_attempts):
        raise ValueError("execution attempt already exists")
    item = ExecutionAttempt(
        task_id,
        attempt,
        ExecutionAttemptStatus.PREPARED,
        recorded_at,
        baseline_head=baseline_head,
        transport=transport,
    )
    return replace(state, execution_attempts=state.execution_attempts + (item,))


def update_attempt(
    state: ProjectState,
    task_id: str,
    attempt: int,
    status: ExecutionAttemptStatus,
    *,
    thread_id: str | None = None,
    turn_id: str | None = None,
    baseline_head: str | None = None,
    terminal_at: datetime | None = None,
    failure_kind: str | None = None,
    partial_paths_exist: bool | None = None,
    transport: TransportDiagnostics | None = None,
) -> ProjectState:
    matches = tuple(
        item
        for item in state.execution_attempts
        if item.task_id == task_id and item.attempt == attempt
    )
    if len(matches) != 1:
        raise ValueError("execution attempt must exist exactly once")
    current = matches[0]
    updated = replace(
        current,
        status=status,
        thread_id=current.thread_id if thread_id is None else thread_id,
        turn_id=current.turn_id if turn_id is None else turn_id,
        baseline_head=(
            current.baseline_head if baseline_head is None else baseline_head
        ),
        terminal_at=terminal_at,
        failure_kind=failure_kind,
        partial_paths_exist=(
            current.partial_paths_exist
            if partial_paths_exist is None
            else partial_paths_exist
        ),
        transport=current.transport if transport is None else transport,
    )
    return replace(
        state,
        execution_attempts=tuple(
            updated if item is current else item for item in state.execution_attempts
        ),
    )


__all__ = ["start_attempt", "update_attempt", "with_safe_point", "with_stop_boundary"]
