"""Pure ProjectState-to-diagnosis classification."""

from __future__ import annotations

from dataclasses import dataclass, replace

from code_mule.domain import (
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.worker_verification import (
    WorkerCheckStatus,
    WorkerCheckType,
    safe_check_name,
)
from code_mule.human import planning_failure_is_persisted
from code_mule.state.models import ProjectState
from code_mule.recovery.service import RecoveryClassifier
from code_mule.revision import latest_revision

from .contracts import (
    DiagnosisBlockerCategory,
    DiagnosisNextAction,
    DiagnosisRecoverability,
    DiagnosisStage,
    ProjectDiagnosis,
    VerificationDiagnosis,
)


@dataclass(frozen=True)
class _Classification:
    category: DiagnosisBlockerCategory
    stage: DiagnosisStage
    summary: str
    recoverability: DiagnosisRecoverability
    boss_action_required: bool
    next_action: DiagnosisNextAction


_HUMAN_ACTIONS = {
    HumanActionCategory.WORKER_INPUT: _Classification(
        DiagnosisBlockerCategory.WORKER_INPUT,
        DiagnosisStage.WORKER_INPUT,
        "Worker is waiting for Boss input.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.WORKER_APPROVAL: _Classification(
        DiagnosisBlockerCategory.WORKER_APPROVAL,
        DiagnosisStage.WORKER_APPROVAL,
        "Worker is waiting for an explicit approval or rejection.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.EXTERNAL_SIDE_EFFECT: _Classification(
        DiagnosisBlockerCategory.EXTERNAL_SIDE_EFFECT,
        DiagnosisStage.EXTERNAL_SIDE_EFFECT,
        "An external side effect is waiting for explicit authorization.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.WORKER_VERIFICATION: _Classification(
        DiagnosisBlockerCategory.WORKER_VERIFICATION,
        DiagnosisStage.WORKER_VERIFICATION,
        "Required Worker verification did not permit Git delivery.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.WORKSPACE_BLOCK: _Classification(
        DiagnosisBlockerCategory.WORKSPACE_BLOCK,
        DiagnosisStage.WORKSPACE_BASELINE,
        "The Git workspace baseline is not safe for Task ownership.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.RECOVERY_UNCERTAIN: _Classification(
        DiagnosisBlockerCategory.RECOVERY_UNCERTAIN,
        DiagnosisStage.EXECUTION_RECOVERY,
        "The outcome of an interrupted execution is uncertain.",
        DiagnosisRecoverability.UNCERTAIN,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.ATTEMPT_LIMIT: _Classification(
        DiagnosisBlockerCategory.ATTEMPT_LIMIT,
        DiagnosisStage.TASK_REVIEW,
        "The Task reached its bounded attempt limit.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.SUPERVISOR_FAILURE: _Classification(
        DiagnosisBlockerCategory.SUPERVISOR_FAILURE,
        DiagnosisStage.TASK_REVIEW,
        "Supervisor processing failed closed.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
    HumanActionCategory.DEPENDENCY_BLOCK: _Classification(
        DiagnosisBlockerCategory.DEPENDENCY_BLOCK,
        DiagnosisStage.DEPENDENCY_RESOLUTION,
        "Task dependencies do not currently permit execution.",
        DiagnosisRecoverability.RECOVERABLE,
        True,
        DiagnosisNextAction.INSPECT,
    ),
}


class ProjectDiagnosisService:
    """Derive bounded Boss-facing facts without I/O or state mutation."""

    def diagnose(self, state: ProjectState) -> ProjectDiagnosis:
        graph = self._active_graph(state)
        inconsistent = graph is None
        plan, tasks = (None, ()) if graph is None else graph
        if plan is None and state.project.status in {
            ProjectStatus.RUNNING,
            ProjectStatus.CHANGE_REQUESTED,
            ProjectStatus.REPLANNING,
            ProjectStatus.CANCEL_REQUESTED,
            ProjectStatus.DONE,
        }:
            inconsistent = True
        current = self._unique_task(state, state.project.current_task_id)
        in_progress = tuple(task for task in tasks if task.status is TaskStatus.IN_PROGRESS)
        if state.project.current_task_id is not None and (
            current is None
            or current not in tasks
            or current.status is not TaskStatus.IN_PROGRESS
        ):
            inconsistent = True
        if (current is None and in_progress) or len(in_progress) > 1:
            inconsistent = True
        pending = tuple(
            action
            for action in state.human_actions
            if action.status is HumanActionStatus.PENDING
        )
        if len(pending) > 1 or (
            state.project.status is ProjectStatus.HUMAN_REQUIRED
            and len(pending) != 1
        ) or (
            state.project.status is not ProjectStatus.HUMAN_REQUIRED
            and pending
        ):
            inconsistent = True

        classification = self._classification(state, pending, inconsistent)
        recovery = RecoveryClassifier().classify(state)
        if state.latest_safe_point is not None and not pending and not inconsistent:
            from dataclasses import replace
            classification = replace(
                classification,
                recoverability=DiagnosisRecoverability(recovery.recoverability.value),
                boss_action_required=recovery.requires_boss_action,
                next_action=(DiagnosisNextAction.RECOVER if recovery.automatic_resume_allowed else classification.next_action),
            )
        completed = tuple(task for task in tasks if task.status is TaskStatus.COMPLETED)
        latest_completed = max(completed, key=lambda task: task.updated_at, default=None)
        action = pending[0] if len(pending) == 1 else None
        revision = latest_revision(state)
        open_change = (
            next(
                (
                    item
                    for item in state.change_requests
                    if item.status
                    in {
                        ChangeRequestStatus.PENDING,
                        ChangeRequestStatus.ANALYZING,
                    }
                ),
                None,
            )
            if state.project.status is ProjectStatus.CHANGE_REQUESTED
            else None
        )
        return ProjectDiagnosis(
            project_name=self._safe(state.project.name, 200),
            project_status=state.project.status,
            active_plan_version=None if plan is None else plan.version,
            completed_tasks=len(completed),
            total_tasks=len(tasks),
            current_task_id=None if current is None else self._safe(current.id, 128),
            current_task_title=None if current is None else self._safe(current.title, 200),
            latest_completed_task_id=(
                None if latest_completed is None else self._safe(latest_completed.id, 128)
            ),
            latest_completed_task_title=(
                None
                if latest_completed is None
                else self._safe(latest_completed.title, 200)
            ),
            latest_task_commit=self._latest_commit(state),
            blocker_category=classification.category,
            blocker_stage=classification.stage,
            blocker_summary=classification.summary,
            recoverability=classification.recoverability,
            boss_action_required=classification.boss_action_required,
            recommended_next_action=classification.next_action,
            pending_action_id=None if action is None else self._safe(action.id, 128),
            worker_input_question=(
                None
                if action is None or action.worker_input is None
                else self._safe(action.worker_input.question, 2_000)
            ),
            verification=(
                None if action is None else self._verification(state, action)
            ),
            last_safe_point=(
                None
                if state.latest_safe_point is None
                else state.latest_safe_point.kind.value
            ),
            stop_reason=(
                None
                if state.latest_execution_stop is None
                else state.latest_execution_stop.reason.value
            ),
            recovery_mode=recovery.recovery_mode.value,
            recovery_command=recovery.next_command,
            revision_number=(
                None if revision is None else revision.revision_number
            ),
            requested_revision=(
                None if open_change is None else open_change.requested_revision
            ),
            base_revision=(
                None if open_change is None else open_change.base_revision
            ),
            base_plan_id=(
                None if open_change is None else open_change.base_plan_id
            ),
            base_plan_version=(
                None
                if open_change is None
                else open_change.base_plan_version
            ),
            change_summary=(
                None if open_change is None else open_change.description
            ),
        )

    def _classification(self, state, pending, inconsistent) -> _Classification:
        if inconsistent:
            return _Classification(
                DiagnosisBlockerCategory.INCONSISTENT_STATE,
                DiagnosisStage.PROJECT_STATE,
                "Persisted project facts are missing, conflicting, or ambiguous.",
                DiagnosisRecoverability.UNCERTAIN,
                True,
                DiagnosisNextAction.INSPECT,
            )
        if pending:
            classification = _HUMAN_ACTIONS.get(
                pending[0].category,
                _Classification(
                    DiagnosisBlockerCategory.INCONSISTENT_STATE,
                    DiagnosisStage.PROJECT_STATE,
                    "The pending Human Action has no deterministic diagnosis.",
                    DiagnosisRecoverability.UNCERTAIN,
                    True,
                    DiagnosisNextAction.INSPECT,
                ),
            )
            if planning_failure_is_persisted(state, pending[0]):
                return replace(classification, stage=DiagnosisStage.PLANNING)
            return classification
        status = state.project.status
        if status is ProjectStatus.PAUSED_BY_BOSS:
            return _Classification(
                DiagnosisBlockerCategory.PAUSED,
                DiagnosisStage.BOSS_CONTROL,
                "Execution was paused by the Boss.",
                DiagnosisRecoverability.RECOVERABLE,
                True,
                DiagnosisNextAction.RESUME,
            )
        if status is ProjectStatus.CHANGE_REQUESTED:
            return _Classification(
                DiagnosisBlockerCategory.CHANGE_REQUESTED,
                DiagnosisStage.REPLANNING,
                "A requirement change is waiting at the Safe Point for impact analysis.",
                DiagnosisRecoverability.RECOVERABLE,
                True,
                DiagnosisNextAction.APPLY_CHANGE,
            )
        if status is ProjectStatus.CANCEL_REQUESTED:
            return _Classification(
                DiagnosisBlockerCategory.CANCELLATION_REQUESTED,
                DiagnosisStage.CANCELLATION,
                "Cancellation is waiting for the active Task Safe Point.",
                DiagnosisRecoverability.NOT_APPLICABLE,
                False,
                DiagnosisNextAction.STATUS,
            )
        if status is ProjectStatus.FAILED:
            return _Classification(
                DiagnosisBlockerCategory.FAILED,
                DiagnosisStage.PROJECT_STATE,
                "The project ended in a failed state.",
                DiagnosisRecoverability.TERMINAL,
                False,
                DiagnosisNextAction.NONE,
            )
        if status is ProjectStatus.DONE:
            summary, recoverability = "Project completed successfully.", DiagnosisRecoverability.TERMINAL
        elif status is ProjectStatus.CANCELLED:
            summary, recoverability = "Project was cancelled; preserved work was not rolled back.", DiagnosisRecoverability.TERMINAL
        elif status is ProjectStatus.RUNNING:
            summary, recoverability = "Project execution is active and no blocker is recorded.", DiagnosisRecoverability.NOT_APPLICABLE
        elif status is ProjectStatus.IDLE:
            summary, recoverability = "Project is initialized and has not started planning.", DiagnosisRecoverability.NOT_APPLICABLE
        elif status is ProjectStatus.PLANNING:
            summary, recoverability = "Project planning is in progress.", DiagnosisRecoverability.NOT_APPLICABLE
        elif status is ProjectStatus.REPLANNING:
            summary, recoverability = "Change impact analysis and replanning are in progress.", DiagnosisRecoverability.NOT_APPLICABLE
        else:
            return _Classification(
                DiagnosisBlockerCategory.INCONSISTENT_STATE,
                DiagnosisStage.PROJECT_STATE,
                "Project status has no deterministic diagnosis.",
                DiagnosisRecoverability.UNCERTAIN,
                True,
                DiagnosisNextAction.INSPECT,
            )
        classification = _Classification(
            DiagnosisBlockerCategory.NONE,
            DiagnosisStage.NONE,
            summary,
            recoverability,
            False,
            DiagnosisNextAction.NONE,
        )
        if status is ProjectStatus.DONE:
            classification = replace(
                classification,
                next_action=DiagnosisNextAction.CHANGE,
            )
        return classification

    @staticmethod
    def _active_graph(state: ProjectState):
        if state.project.active_plan_id is None:
            return (None, ())
        plans = tuple(plan for plan in state.plans if plan.id == state.project.active_plan_id)
        if len(plans) != 1:
            return None
        plan = plans[0]
        milestones = []
        for milestone_id in plan.milestone_ids:
            matches = tuple(item for item in state.milestones if item.id == milestone_id)
            if len(matches) != 1 or matches[0].plan_id != plan.id:
                return None
            milestones.append(matches[0])
        tasks = []
        seen = set()
        for milestone in milestones:
            for task_id in milestone.task_ids:
                matches = tuple(item for item in state.tasks if item.id == task_id)
                if len(matches) != 1 or matches[0].milestone_id != milestone.id or task_id in seen:
                    return None
                seen.add(task_id)
                tasks.append(matches[0])
        return plan, tuple(tasks)

    @staticmethod
    def _unique_task(state: ProjectState, task_id: str | None):
        if task_id is None:
            return None
        matches = tuple(task for task in state.tasks if task.id == task_id)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _safe(value: str, limit: int) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            return "[unavailable]"
        return collapsed[:limit]

    @staticmethod
    def _latest_commit(state: ProjectState) -> str | None:
        if not state.git_commit_results:
            return None
        return ProjectDiagnosisService._safe(
            state.git_commit_results[-1].commit_sha, 128
        )

    @staticmethod
    def _verification(state, action) -> VerificationDiagnosis | None:
        events = tuple(
            event
            for event in state.events
            if event.event_type == "git.delivery_failed"
            and event.entity_id == action.task_id
            and event.timestamp == action.created_at
            and event.metadata.get("error_type") == "WorkerVerificationError"
            and event.metadata.get("stage") == "verification"
        )
        if len(events) != 1:
            return None
        metadata = events[0].metadata
        try:
            return VerificationDiagnosis(
                safe_check_name(metadata["check_name"]),
                WorkerCheckType(metadata["check_type"]),
                WorkerCheckStatus(metadata["check_status"]),
                {"true": True, "false": False}[metadata["check_required"]],
            )
        except (KeyError, ValueError):
            return None


__all__ = ["ProjectDiagnosisService"]
