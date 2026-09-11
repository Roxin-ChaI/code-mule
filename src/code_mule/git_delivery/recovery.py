"""Fail-closed recovery proof for legacy empty-change delivery gates."""

from datetime import UTC, datetime
from pathlib import Path

from code_mule.domain import HumanActionCategory, HumanActionStatus, ProjectStatus
from code_mule.domain.models import HumanAction
from code_mule.execution.contracts import ExecutionLeaseStatus
from code_mule.recovery.contracts import (
    ExecutionAttemptStatus,
    WorkerTerminalState,
)
from code_mule.state.models import ProjectState

from .contracts import (
    GitDeliveryMode,
    GitDeliveryError,
    NoChangeDeliveryRecoveryEvidence,
)
from .service import GitDeliveryService, run_git_command


def _paths(output: str) -> tuple[str, ...] | None:
    values = tuple(sorted(item for item in output.split("\0") if item))
    if len(values) > 1_000 or any(
        len(path) > 240
        or path.startswith("/")
        or path in {".", ".."}
        or ".." in Path(path).parts
        or "\n" in path
        for path in values
    ):
        return None
    return values


def no_change_delivery_recovery_evidence(
    state: ProjectState, action: HumanAction
) -> NoChangeDeliveryRecoveryEvidence | None:
    """Validate one exact historical EmptyGitChangeSet boundary against Git."""

    if (
        action.category is not HumanActionCategory.RECOVERY_UNCERTAIN
        or action.status is not HumanActionStatus.PENDING
        or action.task_id is None
        or state.project.status is not ProjectStatus.HUMAN_REQUIRED
        or state.project.current_task_id != action.task_id
    ):
        return None
    failure = tuple(
        event
        for event in state.events
        if event.event_type == "git.delivery_failed"
        and event.entity_id == action.task_id
        and event.timestamp == action.created_at
        and event.metadata == {
            "error_type": "EmptyGitChangeSet",
            "stage": "ownership",
        }
    )
    attempts = tuple(
        item for item in state.execution_attempts if item.task_id == action.task_id
    )
    if len(failure) != 1 or not attempts:
        return None
    attempt = max(attempts, key=lambda item: item.attempt)
    reports = tuple(
        report
        for report in state.execution_reports
        if report.task_id == action.task_id and report.attempt == attempt.attempt
    )
    baselines = tuple(
        baseline
        for baseline in state.git_baselines
        if baseline.task_id == action.task_id
        and baseline.baseline_head == attempt.baseline_head
    )
    if len(reports) != 1 or len(baselines) != 1:
        return None
    report = reports[0]
    baseline = baselines[0]
    tasks = tuple(task for task in state.tasks if task.id == action.task_id)
    pending = tuple(
        item for item in state.human_actions
        if item.status is HumanActionStatus.PENDING
    )
    root = Path(baseline.repository_root)
    commands = {
        "head": ("git", "rev-parse", "HEAD"),
        "unstaged": ("git", "diff", "--name-only", "-z"),
        "staged": ("git", "diff", "--cached", "--name-only", "-z"),
        "untracked": ("git", "ls-files", "--others", "--exclude-standard", "-z"),
    }
    results = {name: run_git_command(command, root) for name, command in commands.items()}
    current_head = (
        results["head"].stdout.strip()
        if results["head"].returncode == 0
        else None
    )
    unstaged_diff = _paths(results["unstaged"].stdout)
    untracked = _paths(results["untracked"].stdout)
    staged_result = _paths(results["staged"].stdout)
    paths_valid = all(
        value is not None for value in (unstaged_diff, untracked, staged_result)
    )
    unstaged = tuple(
        sorted(dict.fromkeys((unstaged_diff or ()) + (untracked or ())))
    )
    staged = staged_result or ()
    report_valid = False
    if all(result.returncode == 0 for result in results.values()):
        try:
            change_set = GitDeliveryService(
                root, clock=lambda: datetime.now(UTC)
            ).prepare_change_set(baseline, report, report.files_changed)
            report_valid = (
                change_set.delivery_mode is GitDeliveryMode.NO_COMMIT_REQUIRED
            )
        except (OSError, GitDeliveryError):
            report_valid = False
    decisions = tuple(
        decision for decision in state.decisions if decision.task_id == action.task_id
    )
    commits = tuple(
        result for result in state.git_commit_results if result.task_id == action.task_id
    )
    boundary = state.latest_execution_stop
    active_lease = any(
        lease.status is ExecutionLeaseStatus.ACTIVE
        for lease in state.execution_leases
    )
    exact_boundary = (
        boundary is not None
        and boundary.recorded_at == action.created_at
        and boundary.task_id == action.task_id
        and boundary.attempt == attempt.attempt
        and boundary.worker_started
        and boundary.worker_terminal_state is WorkerTerminalState.COMPLETED
        and boundary.report_persisted
    )
    continuation_safe = (
        exact_boundary
        and attempt.status is ExecutionAttemptStatus.REPORT_PERSISTED
        and len(tasks) == 1
        and tasks[0].execution_attempts == attempt.attempt
        and len(pending) == 1
        and pending[0] == action
        and report.status == "completed"
        and report.files_changed == ()
        and report.git_state == "clean"
        and report.human_action is None
        and report_valid
        and paths_valid
        and current_head == baseline.baseline_head
        and baseline.status_entries == ()
        and not unstaged
        and not staged
        and not attempt.partial_paths_exist
        and not decisions
        and not commits
        and not active_lease
    )
    return NoChangeDeliveryRecoveryEvidence(
        task_id=action.task_id,
        attempt=attempt.attempt,
        baseline_head=baseline.baseline_head,
        current_head=current_head,
        reported_paths=report.files_changed,
        actual_unstaged_paths=unstaged,
        actual_staged_paths=staged,
        report_persisted=True,
        supervisor_reviewed=bool(decisions),
        commit_created=bool(commits),
        delivery_mode=GitDeliveryMode.NO_COMMIT_REQUIRED,
        continuation_safe=continuation_safe,
    )


__all__ = ["no_change_delivery_recovery_evidence"]
