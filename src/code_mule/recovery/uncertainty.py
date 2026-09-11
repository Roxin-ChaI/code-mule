"""Read-only projection of persisted uncertain Worker ownership evidence."""

from code_mule.domain import HumanActionCategory, HumanActionStatus
from code_mule.domain.models import HumanAction, ProjectEvent
from code_mule.execution.contracts import ExecutionLeaseStatus
from code_mule.revision import latest_revision
from code_mule.state.models import ProjectState
import math
import re

from .contracts import (
    ExecutionAttemptStatus,
    WorkerOwnershipStatus,
    WorkerStopCause,
    WorkerUncertaintyEvidence,
    WorkerWorkspaceState,
)


def _failure_event(state: ProjectState, action: HumanAction) -> ProjectEvent | None:
    matches = tuple(
        event
        for event in state.events
        if event.event_type == "task.execution_failed"
        and event.entity_id == action.task_id
        and event.timestamp == action.created_at
    )
    return matches[0] if len(matches) == 1 else None


def _turn_failure_fields(
    event: ProjectEvent | None,
) -> tuple[str, int] | None:
    if event is None or event.metadata.get("error_type") != "CodexTurnFailed":
        return None
    metadata = event.metadata
    kind = metadata.get("failure_kind")
    expected_status = {
        "error_notification": None,
        "turn_failed": "failed",
        "turn_interrupted": "interrupted",
    }.get(kind, object())
    if expected_status != metadata.get("turn_status"):
        return None
    if not all(
        re.fullmatch(r"[A-Za-z0-9_-]{1,128}", metadata.get(name, ""))
        for name in ("thread_id", "turn_id")
    ):
        return None
    count = metadata.get("activity_count", "")
    if not count.isascii() or not count.isdigit() or int(count) > 1_000_000_000:
        return None
    try:
        elapsed = float(metadata["turn_elapsed_seconds"])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(elapsed) or not 0 <= elapsed <= 1_000_000_000:
        return None
    if metadata.get("will_retry") not in (None, "false"):
        return None
    return kind, int(count)


def _stop_cause(event: ProjectEvent | None) -> WorkerStopCause:
    if event is None:
        return WorkerStopCause.UNKNOWN
    metadata = event.metadata
    timeout = metadata.get("timeout_kind")
    if timeout == "inactivity":
        return WorkerStopCause.INACTIVITY_TIMEOUT
    if timeout == "hard":
        return WorkerStopCause.HARD_TIMEOUT
    fields = _turn_failure_fields(event)
    if fields is not None:
        try:
            return WorkerStopCause(fields[0])
        except ValueError:
            return WorkerStopCause.UNKNOWN
    return {
        "CodexAppServerStartError": WorkerStopCause.APP_SERVER_START,
        "CodexProtocolError": WorkerStopCause.PROTOCOL_ERROR,
    }.get(metadata.get("error_type"), WorkerStopCause.UNKNOWN)


def _partial_paths(
    state: ProjectState, event: ProjectEvent | None, task_id: str, attempt: int
) -> tuple[tuple[str, ...], bool]:
    if event is not None:
        count = event.metadata.get("partial_path_count")
        if count is not None and count.isascii() and count.isdigit():
            size = int(count)
            if 0 <= size <= 100:
                paths = tuple(
                    event.metadata.get(f"partial_path_{index}", "")
                    for index in range(1, size + 1)
                )
                if all(paths):
                    return paths, True
    # Older schema-v14 evidence did not persist the observed Git path list.
    # A prior trusted report for the same Task is still useful historical path
    # evidence, but it must never be presented as a current-workspace inventory.
    reports = tuple(
        report
        for report in state.execution_reports
        if report.task_id == task_id and report.attempt < attempt
    )
    if reports:
        return max(reports, key=lambda report: report.attempt).files_changed, False
    return (), False


def worker_uncertainty_evidence(
    state: ProjectState, action: HumanAction
) -> WorkerUncertaintyEvidence | None:
    """Return facts only when one pending action maps to one uncertain attempt."""

    if (
        action.category is not HumanActionCategory.RECOVERY_UNCERTAIN
        or action.status is not HumanActionStatus.PENDING
        or action.task_id is None
    ):
        return None
    tasks = tuple(task for task in state.tasks if task.id == action.task_id)
    attempts = tuple(
        item for item in state.execution_attempts if item.task_id == action.task_id
    )
    if len(tasks) != 1 or not attempts:
        return None
    task = tasks[0]
    attempt = max(attempts, key=lambda item: item.attempt)
    if attempt.status not in {
        ExecutionAttemptStatus.UNCERTAIN,
        ExecutionAttemptStatus.INTERRUPTED,
        ExecutionAttemptStatus.FAILED,
    }:
        return None
    event = _failure_event(state, action)
    failure_fields = _turn_failure_fields(event)
    reports = tuple(
        report
        for report in state.execution_reports
        if report.task_id == task.id and report.attempt == attempt.attempt
    )
    commits = tuple(
        result
        for result in state.git_commit_results
        if result.task_id == task.id and result.committed_at >= attempt.started_at
    )
    partial_paths, complete = _partial_paths(
        state, event, task.id, attempt.attempt
    )
    matching_leases = tuple(
        lease
        for lease in state.execution_leases
        if lease.current_task_id == task.id
        and lease.attempt == attempt.attempt
        and (
            attempt.thread_id is None
            or lease.codex_thread_id == attempt.thread_id
        )
    )
    any_task_lease = any(
        lease.current_task_id == task.id and lease.attempt == attempt.attempt
        for lease in state.execution_leases
    )
    if matching_leases:
        lease = max(matching_leases, key=lambda item: item.acquired_at)
        ownership = (
            WorkerOwnershipStatus.ACTIVE_MATCHED
            if lease.status is ExecutionLeaseStatus.ACTIVE
            else WorkerOwnershipStatus.RELEASED_MATCHED
            if lease.status is ExecutionLeaseStatus.RELEASED
            else WorkerOwnershipStatus.IDENTITY_MISMATCH
        )
    elif any_task_lease:
        ownership = WorkerOwnershipStatus.IDENTITY_MISMATCH
    else:
        ownership = WorkerOwnershipStatus.UNAVAILABLE
    started = attempt.status is not ExecutionAttemptStatus.PREPARED
    activity_count = 0 if failure_fields is None else failure_fields[1]
    last_trusted = (
        "Worker activity observed"
        if activity_count
        else "Worker started"
        if started
        else "Git baseline captured"
    )
    revision = latest_revision(state)
    active_plan = next(
        (plan for plan in state.plans if plan.id == state.project.active_plan_id),
        None,
    )
    commit = max(commits, key=lambda item: item.committed_at, default=None)
    workspace = (
        WorkerWorkspaceState.CHANGED
        if attempt.partial_paths_exist
        else WorkerWorkspaceState.CLEAN
        if attempt.baseline_head is not None
        else WorkerWorkspaceState.UNKNOWN
    )
    # A failure notification is a trusted stop cause, not a successful terminal
    # turn result. It cannot prove whether observed side effects completed.
    terminal_result = bool(
        failure_fields is not None
        and failure_fields[0] in {"turn_failed", "turn_interrupted"}
    )
    return WorkerUncertaintyEvidence(
        revision_number=None if revision is None else revision.revision_number,
        plan_version=None if active_plan is None else active_plan.version,
        task_id=task.id,
        task_title=task.title,
        attempt=attempt.attempt,
        worker_started=started,
        last_trusted_stage=last_trusted,
        trusted_terminal_result=terminal_result,
        report_persisted=len(reports) == 1,
        workspace_state=workspace,
        partial_paths=partial_paths,
        partial_paths_complete=complete,
        commit_created=commit is not None,
        commit_sha=None if commit is None else commit.commit_sha,
        ownership_status=ownership,
        stop_cause=_stop_cause(event),
        retry_safe=False,
    )


__all__ = ["worker_uncertainty_evidence"]
