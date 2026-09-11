"""Deterministic, action-scoped human resolution operations."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import (
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import (
    HumanAction,
    HumanResolution,
    ProjectEvent,
)
from code_mule.domain.state_machine import validate_transition
from code_mule.execution.contracts import ExecutionLeaseStatus
from code_mule.recovery.contracts import ExecutionPhase, ExecutionStopReason
from code_mule.state.models import ProjectState


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...
    def save(self, state: ProjectState) -> None: ...


class HumanResolutionError(RuntimeError):
    """Base class for fail-closed human resolution errors."""


class HumanActionNotFound(HumanResolutionError):
    """Raised when an action ID is not the unique pending action."""


class InvalidHumanResolution(HumanResolutionError):
    """Raised when an action cannot accept the requested operation."""


_APPROVABLE = frozenset(
    {
        HumanActionCategory.WORKER_APPROVAL,
        HumanActionCategory.EXTERNAL_SIDE_EFFECT,
    }
)
_RETRYABLE = frozenset(
    {
        HumanActionCategory.ATTEMPT_LIMIT,
        HumanActionCategory.DEPENDENCY_BLOCK,
        HumanActionCategory.WORKSPACE_BLOCK,
    }
)


def planning_failure_is_persisted(
    state: ProjectState, action: HumanAction
) -> bool:
    """Identify one exact persisted initial-planning Human Gate."""

    boundary = state.latest_execution_stop
    matching_events = tuple(
        event
        for event in state.events
        if event.event_type in {"planning.failed", "planning.proposal_rejected"}
        and event.entity_id == state.project.id
        and event.timestamp == action.created_at
    )
    return (
        action.category is HumanActionCategory.SUPERVISOR_FAILURE
        and action.status is HumanActionStatus.PENDING
        and action.task_id is None
        and state.project.status is ProjectStatus.HUMAN_REQUIRED
        and boundary is not None
        and boundary.reason is ExecutionStopReason.SUPERVISOR_FAILED
        # PROJECT was emitted by schema-v12 clients before planning gained its
        # own typed phase. The exact planning event keeps this compatibility
        # path deterministic and scoped to the same failure boundary.
        and boundary.phase in {ExecutionPhase.PLANNING, ExecutionPhase.PROJECT}
        and boundary.recorded_at == action.created_at
        and not boundary.worker_started
        and len(matching_events) == 1
    )


def planning_retry_is_safe(state: ProjectState, action: HumanAction) -> bool:
    """Return whether an explicit fresh PLAN can start without duplicating work."""

    return (
        planning_failure_is_persisted(state, action)
        and state.project.objective not in (None, "")
        and state.project.active_plan_id is None
        and state.project.current_task_id is None
        and not state.plans
        and not state.milestones
        and not state.tasks
        and not state.revisions
        and not state.execution_attempts
        and not state.execution_reports
        and not state.decisions
        and not state.git_baselines
        and not state.git_change_sets
        and not state.git_commit_results
        and not state.project_verification_results
        and not any(
            lease.status is ExecutionLeaseStatus.ACTIVE
            for lease in state.execution_leases
        )
    )


def post_completion_replanning_failure_evidence(
    state: ProjectState, action: HumanAction
):
    """Load post-completion evidence lazily to avoid package import cycles."""

    from code_mule.replanning.recovery import (
        post_completion_replanning_failure_evidence as classify,
    )

    return classify(state, action)


def post_completion_replanning_failure_is_persisted(
    state: ProjectState, action: HumanAction
) -> bool:
    from code_mule.replanning.recovery import (
        post_completion_replanning_failure_is_persisted as classify,
    )

    return classify(state, action)


def post_completion_replanning_retry_is_safe(
    state: ProjectState, action: HumanAction
) -> bool:
    from code_mule.replanning.recovery import (
        post_completion_replanning_retry_safety,
    )

    return post_completion_replanning_retry_safety(
        state, action, validate_workspace=True
    ).safe


def allowed_resolution_strategies(
    state: ProjectState, action: HumanAction
) -> tuple[HumanResolutionStrategy, ...]:
    """List strategies accepted by ``resolve`` for this exact persisted gate."""

    if (
        action.status is not HumanActionStatus.PENDING
        or action.category in _APPROVABLE
    ):
        return ()
    strategies: list[HumanResolutionStrategy] = []
    if action.category in _RETRYABLE and action.task_id is not None:
        strategies.append(HumanResolutionStrategy.RETRY_TASK)
    if planning_retry_is_safe(state, action):
        strategies.append(HumanResolutionStrategy.RETRY_PLANNING)
    if post_completion_replanning_retry_is_safe(state, action):
        strategies.append(HumanResolutionStrategy.RETRY_REPLANNING)
    from code_mule.git_delivery.recovery import (
        no_change_delivery_recovery_evidence,
    )

    no_change = no_change_delivery_recovery_evidence(state, action)
    if no_change is not None and no_change.continuation_safe:
        strategies.append(HumanResolutionStrategy.CONTINUE_AFTER_REPORT)
    acknowledged = any(
        resolution.action_id == action.id
        and resolution.strategy is HumanResolutionStrategy.ACKNOWLEDGE
        for resolution in state.human_resolutions
    )
    strategies.extend(
        (
            HumanResolutionStrategy.FAIL_PROJECT,
        )
    )
    if not acknowledged:
        strategies.append(HumanResolutionStrategy.ACKNOWLEDGE)
    return tuple(strategies)


class HumanResolutionService:
    """Close exactly one pending action and persist its audit boundary."""

    def __init__(
        self,
        store: ProjectStateStore,
        *,
        clock: Callable[[], datetime],
        event_id_factory: Callable[[], str],
        resolution_id_factory: Callable[[], str],
    ) -> None:
        self._store = store
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._resolution_id_factory = resolution_id_factory

    def approve(self, action_id: str) -> ProjectState:
        state, action = self._target(action_id)
        if action.category not in _APPROVABLE:
            raise InvalidHumanResolution(
                f"{action.category.value} must be handled with resolve"
            )
        return self._close_action(
            state,
            action,
            HumanActionStatus.APPROVED,
            "human_action.approved",
            HumanResolutionStrategy.APPROVE,
        )

    def reject(self, action_id: str) -> ProjectState:
        state, action = self._target(action_id)
        return self._close_action(
            state,
            action,
            HumanActionStatus.REJECTED,
            "human_action.rejected",
            HumanResolutionStrategy.REJECT,
        )

    def answer(self, action_id: str, answer: str) -> ProjectState:
        state, action = self._target(action_id)
        if action.category is not HumanActionCategory.WORKER_INPUT:
            raise InvalidHumanResolution(
                f"{action.category.value} cannot accept a Worker answer"
            )
        details = action.worker_input
        if details is None or details.baseline_head is None:
            raise InvalidHumanResolution(
                "Worker input continuation evidence is incomplete"
            )
        if answer.strip() == "":
            raise InvalidHumanResolution("answer is empty or exceeds safe bounds")
        try:
            answered_details = replace(details, answer=answer)
        except ValueError as error:
            raise InvalidHumanResolution(
                "answer is empty or exceeds safe bounds"
            ) from error
        if action.task_id is None:
            raise InvalidHumanResolution("Worker input action has no Task")
        matches = tuple(task for task in state.tasks if task.id == action.task_id)
        if len(matches) != 1:
            raise InvalidHumanResolution("HumanAction task is unavailable")
        task = matches[0]
        if task.status is not TaskStatus.IN_PROGRESS:
            raise InvalidHumanResolution(
                "Worker input requires a stopped IN_PROGRESS Task"
            )

        operation_time = self._clock()
        validate_transition(state.project.status, ProjectStatus.RUNNING)
        reopened = replace(
            task, status=TaskStatus.REOPENED, updated_at=operation_time
        )
        closed = replace(
            action,
            status=HumanActionStatus.RESOLVED,
            resolved_at=operation_time,
            worker_input=answered_details,
        )
        resolution = HumanResolution(
            id=self._resolution_id_factory(),
            action_id=action.id,
            project_id=state.project.id,
            strategy=HumanResolutionStrategy.ANSWER,
            summary="Boss answered the exact pending Worker input request",
            created_at=operation_time,
        )
        event = self._event(
            state,
            action,
            "human_action.answered",
            operation_time,
            {"resolution_id": resolution.id, "strategy": "answer"},
        )
        updated = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.RUNNING,
                current_task_id=None,
                updated_at=operation_time,
            ),
            tasks=tuple(
                reopened if item.id == reopened.id else item for item in state.tasks
            ),
            human_actions=self._replace_action(state.human_actions, closed),
            human_resolutions=state.human_resolutions + (resolution,),
            events=state.events + (event,),
        )
        self._store.save(updated)
        return updated

    def resolve(
        self, action_id: str, strategy: HumanResolutionStrategy
    ) -> ProjectState:
        state, action = self._target(action_id)
        if action.category in _APPROVABLE:
            raise InvalidHumanResolution(
                f"{action.category.value} must be approved or rejected"
            )
        if strategy not in allowed_resolution_strategies(state, action):
            raise InvalidHumanResolution(
                f"{strategy.value} is not safe; strategy is not allowed "
                "for this HumanAction"
            )
        operation_time = self._clock()
        project = state.project
        tasks = state.tasks
        if strategy is HumanResolutionStrategy.RETRY_TASK:
            if action.category not in _RETRYABLE or action.task_id is None:
                raise InvalidHumanResolution(
                    "retry_task is not safe for this HumanAction"
                )
            matches = tuple(task for task in tasks if task.id == action.task_id)
            if len(matches) != 1:
                raise InvalidHumanResolution("HumanAction task is unavailable")
            task = matches[0]
            if task.status not in {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}:
                raise InvalidHumanResolution("retry_task requires a stopped Task")
            retried = replace(
                task, status=TaskStatus.REOPENED, updated_at=operation_time
            )
            tasks = tuple(retried if item.id == retried.id else item for item in tasks)
            validate_transition(project.status, ProjectStatus.RUNNING)
            project = replace(
                project,
                status=ProjectStatus.RUNNING,
                current_task_id=None,
                updated_at=operation_time,
            )
            summary = "Boss explicitly reopened the stopped Task"
        elif strategy is HumanResolutionStrategy.RETRY_PLANNING:
            validate_transition(project.status, ProjectStatus.PLANNING)
            project = replace(
                project,
                status=ProjectStatus.PLANNING,
                active_plan_id=None,
                current_task_id=None,
                updated_at=operation_time,
            )
            summary = "Boss explicitly authorized a fresh initial planning call"
        elif strategy is HumanResolutionStrategy.RETRY_REPLANNING:
            from code_mule.replanning.recovery import (
                post_completion_replanning_retry_safety,
            )

            safety = post_completion_replanning_retry_safety(
                state, action, validate_workspace=True
            )
            if not safety.safe or safety.evidence is None:
                raise InvalidHumanResolution(
                    "retry_replanning safety proof no longer holds"
                )
            change = safety.evidence.change_request
            validate_transition(project.status, ProjectStatus.CHANGE_REQUESTED)
            project = replace(
                project,
                status=ProjectStatus.CHANGE_REQUESTED,
                current_task_id=None,
                updated_at=operation_time,
            )
            pending_change = replace(
                change, status=ChangeRequestStatus.PENDING
            )
            state = replace(
                state,
                change_requests=tuple(
                    pending_change if item.id == change.id else item
                    for item in state.change_requests
                ),
            )
            summary = (
                "Boss explicitly authorized fresh post-completion replanning"
            )
        elif strategy is HumanResolutionStrategy.CONTINUE_AFTER_REPORT:
            from code_mule.git_delivery.recovery import (
                no_change_delivery_recovery_evidence,
            )

            evidence = no_change_delivery_recovery_evidence(state, action)
            if evidence is None or not evidence.continuation_safe:
                raise InvalidHumanResolution(
                    "continue_after_report safety proof no longer holds"
                )
            validate_transition(project.status, ProjectStatus.RUNNING)
            project = replace(
                project,
                status=ProjectStatus.RUNNING,
                current_task_id=evidence.task_id,
                updated_at=operation_time,
            )
            summary = (
                "Boss authorized review of the trusted zero-change Worker report"
            )
        elif strategy is HumanResolutionStrategy.FAIL_PROJECT:
            validate_transition(project.status, ProjectStatus.FAILED)
            project = replace(
                project,
                status=ProjectStatus.FAILED,
                current_task_id=None,
                updated_at=operation_time,
            )
            summary = "Boss explicitly stopped the project"
        elif strategy is HumanResolutionStrategy.ACKNOWLEDGE:
            summary = "Boss acknowledged the action; project remains safely stopped"
            if action.category is HumanActionCategory.RECOVERY_UNCERTAIN:
                resolution = HumanResolution(
                    id=self._resolution_id_factory(),
                    action_id=action.id,
                    project_id=state.project.id,
                    strategy=strategy,
                    summary=(
                        "Boss acknowledged uncertain Worker ownership; "
                        "the Human Gate remains pending"
                    ),
                    created_at=operation_time,
                )
                event = self._event(
                    state,
                    action,
                    "human_action.acknowledged",
                    operation_time,
                    {"strategy": strategy.value, "resolution_id": resolution.id},
                )
                updated = replace(
                    state,
                    project=replace(state.project, updated_at=operation_time),
                    human_resolutions=state.human_resolutions + (resolution,),
                    events=state.events + (event,),
                )
                self._store.save(updated)
                return updated
            if action.category is HumanActionCategory.FINAL_REVIEW_DECISION:
                validate_transition(project.status, ProjectStatus.PAUSED_BY_BOSS)
                project = replace(
                    project,
                    status=ProjectStatus.PAUSED_BY_BOSS,
                    current_task_id=None,
                    updated_at=operation_time,
                )
        else:
            raise InvalidHumanResolution("unsupported resolution strategy")

        resolution = HumanResolution(
            id=self._resolution_id_factory(),
            action_id=action.id,
            project_id=state.project.id,
            strategy=strategy,
            summary=summary,
            created_at=operation_time,
        )
        closed = replace(
            action,
            status=HumanActionStatus.RESOLVED,
            resolved_at=operation_time,
        )
        event = self._event(
            state,
            action,
            "human_action.resolved",
            operation_time,
            {"strategy": strategy.value, "resolution_id": resolution.id},
        )
        updated = replace(
            state,
            project=project,
            tasks=tasks,
            human_actions=self._replace_action(state.human_actions, closed),
            human_resolutions=state.human_resolutions + (resolution,),
            events=state.events + (event,),
        )
        self._store.save(updated)
        return updated

    def _target(self, action_id: str) -> tuple[ProjectState, HumanAction]:
        state = self._store.load()
        if state.project.status is not ProjectStatus.HUMAN_REQUIRED:
            raise InvalidHumanResolution("project is not HUMAN_REQUIRED")
        matches = tuple(action for action in state.human_actions if action.id == action_id)
        if len(matches) != 1:
            raise HumanActionNotFound("HumanAction is unknown")
        action = matches[0]
        if action.status is not HumanActionStatus.PENDING:
            raise InvalidHumanResolution("HumanAction is already closed")
        pending = tuple(
            item for item in state.human_actions if item.status is HumanActionStatus.PENDING
        )
        if pending != (action,):
            raise InvalidHumanResolution("project must have one unique pending HumanAction")
        return state, action

    def _close_action(
        self,
        state: ProjectState,
        action: HumanAction,
        status: HumanActionStatus,
        event_type: str,
        strategy: HumanResolutionStrategy,
    ) -> ProjectState:
        operation_time = self._clock()
        closed = replace(action, status=status, resolved_at=operation_time)
        project = replace(state.project, updated_at=operation_time)
        tasks = state.tasks
        expiry_event: ProjectEvent | None = None
        capability = action.capability_approval
        if capability is not None and not capability.native_request_active:
            validate_transition(state.project.status, ProjectStatus.FAILED)
            project = replace(
                project,
                status=ProjectStatus.FAILED,
                current_task_id=None,
            )
            if action.task_id is not None:
                matching = tuple(task for task in tasks if task.id == action.task_id)
                if len(matching) != 1:
                    raise InvalidHumanResolution(
                        "capability approval Task is unavailable"
                    )
                blocked = replace(
                    matching[0], status=TaskStatus.BLOCKED, updated_at=operation_time
                )
                tasks = tuple(
                    blocked if task.id == blocked.id else task for task in tasks
                )
            expiry_event = self._event(
                state,
                action,
                "worker.capability_approval_expired",
                operation_time,
                {
                    "native_delivery": "unavailable",
                    "request_method": capability.request_method,
                },
            )
        resolution = HumanResolution(
            id=self._resolution_id_factory(),
            action_id=action.id,
            project_id=state.project.id,
            strategy=strategy,
            summary=(
                "Boss approved the exact pending action"
                if strategy is HumanResolutionStrategy.APPROVE
                else "Boss rejected the exact pending action"
            ),
            created_at=operation_time,
        )
        event = self._event(
            state,
            action,
            event_type,
            operation_time,
            {"resolution_id": resolution.id},
        )
        updated = replace(
            state,
            project=project,
            tasks=tasks,
            human_actions=self._replace_action(state.human_actions, closed),
            human_resolutions=state.human_resolutions + (resolution,),
            events=state.events
            + (() if expiry_event is None else (expiry_event,))
            + (event,),
        )
        self._store.save(updated)
        return updated

    def _event(
        self,
        state: ProjectState,
        action: HumanAction,
        event_type: str,
        timestamp: datetime,
        metadata: dict[str, str],
    ) -> ProjectEvent:
        return ProjectEvent(
            id=self._event_id_factory(),
            project_id=state.project.id,
            event_type=event_type,
            entity_id=action.id,
            timestamp=timestamp,
            metadata={
                "action_id": action.id,
                "category": action.category.value,
                **metadata,
            },
        )

    @staticmethod
    def _replace_action(
        actions: tuple[HumanAction, ...], updated: HumanAction
    ) -> tuple[HumanAction, ...]:
        return tuple(updated if action.id == updated.id else action for action in actions)


__all__ = [
    "HumanActionNotFound",
    "HumanResolutionError",
    "HumanResolutionService",
    "InvalidHumanResolution",
    "ProjectStateStore",
    "allowed_resolution_strategies",
    "planning_failure_is_persisted",
    "planning_retry_is_safe",
]
