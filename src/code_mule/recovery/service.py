"""Pure recovery classification plus deterministic workspace preflight."""

from pathlib import Path
import subprocess

from code_mule.domain import HumanActionCategory, HumanActionStatus, ProjectStatus, TaskStatus
from code_mule.state.models import ProjectState

from .contracts import (
    BoundaryRecoverability,
    ExecutionAttemptStatus,
    RecoveryMode,
    RecoveryPlan,
    SafePointKind,
)


class RecoveryPreflightError(ValueError):
    """Raised when persisted recovery facts do not match the repository."""


class RecoveryClassifier:
    """Classify recovery from persisted facts without mutating state."""

    def classify(self, state: ProjectState, *, validate_workspace: bool = False) -> RecoveryPlan:
        plan = self._classify(state)
        if validate_workspace and plan.requires_workspace_validation:
            self._validate_workspace(state, plan)
        return plan

    def _classify(self, state: ProjectState) -> RecoveryPlan:
        point = state.latest_safe_point
        safe = SafePointKind.UNCERTAIN if point is None else point.kind
        pending = tuple(
            action for action in state.human_actions
            if action.status is HumanActionStatus.PENDING
        )
        if pending:
            return self._plan(
                RecoveryMode.BLOCKED, safe, BoundaryRecoverability.UNCERTAIN,
                True, False, False, False,
                "A pending HumanAction must be resolved explicitly.", "code-mule inspect",
                state.project.current_task_id,
            )
        if state.project.status in {ProjectStatus.DONE, ProjectStatus.CANCELLED, ProjectStatus.FAILED}:
            return self._plan(
                RecoveryMode.NOT_NEEDED, safe, BoundaryRecoverability.TERMINAL,
                False, False, False, False,
                "The project is in a terminal state.", "code-mule status",
            )
        if state.project.status is ProjectStatus.PAUSED_BY_BOSS:
            return self._plan(
                RecoveryMode.NOT_NEEDED, safe, BoundaryRecoverability.RECOVERABLE,
                True, False, False, False,
                "Boss pause is resumed with the control command.", "code-mule resume",
            )
        if state.project.status is ProjectStatus.PLANNING:
            if state.project.active_plan_id is None:
                return self._plan(
                    RecoveryMode.FRESH_PLANNING, safe, BoundaryRecoverability.RECOVERABLE,
                    False, False, False, True,
                    "No Plan was materialized; a fresh planning call is required.",
                    "code-mule recover",
                )
            return self._plan(
                RecoveryMode.RESUME_PLAN, SafePointKind.PLAN_MATERIALIZED,
                BoundaryRecoverability.RECOVERABLE, False, False, False, True,
                "The materialized active Plan is reused.", "code-mule recover",
            )
        task_id = state.project.current_task_id
        if task_id is not None:
            task = self._task(state, task_id)
            if task.status is not TaskStatus.IN_PROGRESS:
                return self._blocked(safe, task_id, "Current Task lifecycle is inconsistent.")
            attempts = tuple(item for item in state.execution_attempts if item.task_id == task_id)
            if not attempts:
                return self._plan(
                    RecoveryMode.DISPATCH_FRESH_WORKER, safe,
                    BoundaryRecoverability.RECOVERABLE, False, False, True, True,
                    "The Task was selected but no Worker attempt was started.",
                    "code-mule recover", task_id,
                )
            latest = max(attempts, key=lambda item: item.attempt)
            if latest.status is ExecutionAttemptStatus.REPORT_PERSISTED:
                return self._plan(
                    RecoveryMode.CONTINUE_AFTER_REPORT, SafePointKind.TASK_WORKER_COMPLETED,
                    BoundaryRecoverability.RECOVERABLE, False, True, False, True,
                    "The trusted Worker report is persisted; resume at review.",
                    "code-mule recover", task_id, latest.attempt,
                )
            if latest.status is ExecutionAttemptStatus.PREPARED:
                return self._plan(
                    RecoveryMode.DISPATCH_FRESH_WORKER, SafePointKind.TASK_BASELINE_CAPTURED,
                    BoundaryRecoverability.RECOVERABLE, False, True, True, True,
                    "The Worker never started after the Git baseline.",
                    "code-mule recover", task_id, latest.attempt,
                )
            return self._blocked(
                safe, task_id,
                "Worker started without a trusted recoverable terminal boundary.",
                latest.attempt,
            )
        answered = tuple(
            action for action in state.human_actions
            if action.category is HumanActionCategory.WORKER_INPUT
            and action.status is HumanActionStatus.RESOLVED
            and action.worker_input is not None
            and action.worker_input.answer is not None
        )
        if state.project.status is ProjectStatus.RUNNING and answered:
            action = answered[-1]
            return self._plan(
                RecoveryMode.CONTINUE_AFTER_INPUT, SafePointKind.HUMAN_GATE,
                BoundaryRecoverability.RECOVERABLE, False, True, True, True,
                "Worker input was answered; continue the same Task in a fresh session.",
                "code-mule recover", action.task_id, action.worker_input.worker_attempt,
            )
        workspace_resolutions = tuple(
            action for action in state.human_actions
            if action.category is HumanActionCategory.WORKSPACE_BLOCK
            and action.status is HumanActionStatus.RESOLVED
        )
        if state.project.status is ProjectStatus.RUNNING and workspace_resolutions:
            return self._plan(
                RecoveryMode.RESUME_PLAN, safe, BoundaryRecoverability.RECOVERABLE,
                False, True, False, True,
                "Workspace block was resolved; verify a clean baseline before dispatch.",
                "code-mule recover", workspace_resolutions[-1].task_id,
            )
        if state.project.status is ProjectStatus.RUNNING and state.project.active_plan_id:
            return self._plan(
                RecoveryMode.RESUME_PLAN, safe, BoundaryRecoverability.RECOVERABLE,
                False, False, False, True,
                "Continue the persisted active Plan without replanning.", "code-mule recover",
            )
        return self._blocked(safe, task_id, "Persisted recovery facts are insufficient.")

    @staticmethod
    def _task(state: ProjectState, task_id: str):
        matches = tuple(item for item in state.tasks if item.id == task_id)
        if len(matches) != 1:
            raise RecoveryPreflightError("current Task must exist exactly once")
        return matches[0]

    @staticmethod
    def _plan(mode, safe, recoverability, boss, workspace, fresh, allowed, reason, command, task_id=None, attempt=None):
        return RecoveryPlan(mode, safe, recoverability, boss, workspace, fresh, allowed, reason, command, task_id, attempt)

    def _blocked(self, safe, task_id, reason, attempt=None):
        return self._plan(
            RecoveryMode.BLOCKED, safe, BoundaryRecoverability.UNCERTAIN,
            True, True, False, False, reason, "code-mule diagnose", task_id, attempt,
        )

    def _validate_workspace(self, state: ProjectState, plan: RecoveryPlan) -> None:
        workspace = state.project.workspace
        if workspace is None:
            raise RecoveryPreflightError("workspace is unavailable")
        root = Path(workspace)
        if not root.is_absolute() or not root.is_dir():
            raise RecoveryPreflightError("workspace is invalid")
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=root, text=True,
            capture_output=True, check=False,
        )
        if completed.returncode != 0:
            raise RecoveryPreflightError("workspace HEAD is unavailable")
        attempts = tuple(
            item for item in state.execution_attempts
            if item.task_id == plan.task_id and item.attempt == plan.attempt
        )
        expected = attempts[0].baseline_head if len(attempts) == 1 else None
        if expected is not None and completed.stdout.strip() != expected:
            raise RecoveryPreflightError("workspace HEAD drifted from the persisted baseline")
        status = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=root, text=True, capture_output=True, check=False,
        )
        if status.returncode != 0:
            raise RecoveryPreflightError("workspace status is unavailable")
        staged = tuple(line for line in status.stdout.splitlines() if line and line[0] not in {" ", "?"})
        if staged:
            raise RecoveryPreflightError("workspace contains staged paths")
        if plan.recovery_mode is RecoveryMode.CONTINUE_AFTER_REPORT:
            reports = tuple(item for item in state.execution_reports if item.task_id == plan.task_id)
            if not reports:
                raise RecoveryPreflightError("persisted Worker report is unavailable")
            actual = {
                line[3:] for line in status.stdout.splitlines()
                if len(line) >= 4 and line[:2] != "!!"
            }
            if actual != set(reports[-1].files_changed):
                raise RecoveryPreflightError("workspace paths differ from the persisted Worker report")
        elif plan.recovery_mode is RecoveryMode.CONTINUE_AFTER_INPUT:
            actions = tuple(
                action for action in state.human_actions
                if action.task_id == plan.task_id
                and action.category is HumanActionCategory.WORKER_INPUT
                and action.status is HumanActionStatus.RESOLVED
                and action.worker_input is not None
            )
            if len(actions) != 1:
                raise RecoveryPreflightError("Worker input continuation is ambiguous")
            actual = {
                line[3:] for line in status.stdout.splitlines()
                if len(line) >= 4 and line[:2] != "!!"
            }
            if actual != set(actions[0].worker_input.partial_paths):
                raise RecoveryPreflightError("workspace paths differ from the Worker input boundary")
        elif plan.recovery_mode is RecoveryMode.RESUME_PLAN and status.stdout:
            raise RecoveryPreflightError("workspace is not clean after resolving its block")


__all__ = ["RecoveryClassifier", "RecoveryPreflightError"]
