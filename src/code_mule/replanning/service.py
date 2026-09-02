"""Deterministic orchestration of one safe-point change replan."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import (
    ChangeRequestStatus,
    HumanActionCategory,
    PlanStatus,
    ProjectStatus,
)
from code_mule.domain.models import ChangeRequest, Plan, ProjectEvent
from code_mule.domain.state_machine import validate_transition
from code_mule.human import request_human_action
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.state.models import ProjectState
from code_mule.supervisor import ImpactAnalysisRequest, ImpactAnalysisResult

from .contracts import ChangeReplanningOutcome, ChangeReplanningRequest
from .errors import (
    InvalidReplanProposal,
    InvalidReplanningState,
    ReplanMaterializationError,
    SupervisorReplanningError,
)
from .materialization import ChangeReplanMaterializer
from .validation import ChangeReplanValidator


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...

    def save(self, state: ProjectState) -> None: ...


class ImpactSupervisor(Protocol):
    def analyze_change(
        self, request: ImpactAnalysisRequest
    ) -> ImpactAnalysisResult: ...


class ChangeReplanningService:
    """Persist REPLANNING, accept one proposal, and atomically replace the Plan."""

    def __init__(
        self,
        *,
        store: ProjectStateStore,
        supervisor: ImpactSupervisor,
        clock: Callable[[], datetime],
        plan_id_factory: Callable[[], str],
        event_id_factory: Callable[[], str],
        progress_sink: ProgressSink | None = None,
        validator: ChangeReplanValidator | None = None,
        materializer: ChangeReplanMaterializer | None = None,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._clock = clock
        self._plan_id_factory = plan_id_factory
        self._event_id_factory = event_id_factory
        self._progress = resilient_progress_sink(progress_sink)
        self._validator = validator or ChangeReplanValidator()
        self._materializer = materializer or ChangeReplanMaterializer(
            self._validator
        )

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def replan(
        self, request: ChangeReplanningRequest
    ) -> ChangeReplanningOutcome:
        initial = self._store.load()
        change_request, active_plan = self._validate_start(initial, request)
        replanning = self._start_replanning(initial, change_request)
        analyzing_change = self._change_request(
            replanning, change_request.id
        )
        self._emit(
            replanning,
            ProgressEventType.REPLANNING_STARTED,
            "Change replanning started",
        )
        self._emit(
            replanning,
            ProgressEventType.SUPERVISOR_IMPACT_STARTED,
            "Supervisor analyzing impact...",
        )
        try:
            proposal = self._supervisor.analyze_change(
                ImpactAnalysisRequest(replanning, analyzing_change)
            )
        except BaseException as error:
            self._fail(
                replanning,
                analyzing_change,
                event_type="replanning.failed",
                error_type=type(error).__name__,
            )
            raise SupervisorReplanningError(
                "Supervisor Impact Analysis failed"
            ) from error

        self._emit(
            replanning,
            ProgressEventType.SUPERVISOR_IMPACT_COMPLETED,
            "Supervisor impact proposal received",
        )
        try:
            self._validator.validate(replanning, analyzing_change, proposal)
        except InvalidReplanProposal as error:
            self._fail(
                replanning,
                analyzing_change,
                event_type="replanning.proposal_rejected",
                error_type=type(error).__name__,
            )
            raise

        self._emit(
            replanning,
            ProgressEventType.REPLANNING_MATERIALIZING,
            "Materializing replacement Plan",
        )
        plan_id = self._plan_id_factory()
        operation_time = self._clock()
        try:
            materialized = self._materializer.materialize(
                replanning,
                analyzing_change,
                proposal,
                plan_id=plan_id,
                operation_time=operation_time,
            )
        except (InvalidReplanProposal, ReplanMaterializationError) as error:
            self._fail(
                replanning,
                analyzing_change,
                event_type="replanning.failed",
                error_type=type(error).__name__,
            )
            raise

        plan = next(item for item in materialized.plans if item.id == plan_id)
        final = replace(
            materialized,
            events=materialized.events
            + (
                self._event(
                    materialized,
                    "supervisor.impact_completed",
                    analyzing_change.id,
                    operation_time,
                    {},
                ),
                self._event(
                    materialized,
                    "replanning.materializing",
                    plan.id,
                    operation_time,
                    {},
                ),
                self._event(
                    materialized,
                    "replanning.completed",
                    plan.id,
                    operation_time,
                    {
                        "previous_plan_id": active_plan.id,
                        "previous_plan_version": str(active_plan.version),
                        "plan_version": str(plan.version),
                    },
                ),
            ),
        )
        # This is the only save that exposes the replacement graph as RUNNING.
        self._store.save(final)
        self._emit(
            final,
            ProgressEventType.REPLANNING_COMPLETED,
            f"Plan v{active_plan.version} → v{plan.version}; execution resumed",
            metadata={
                "previous_plan_id": active_plan.id,
                "plan_id": plan.id,
                "previous_plan_version": str(active_plan.version),
                "plan_version": str(plan.version),
            },
        )
        return ChangeReplanningOutcome(
            project_id=final.project.id,
            change_request_id=analyzing_change.id,
            previous_plan_id=active_plan.id,
            plan_id=plan.id,
            previous_plan_version=active_plan.version,
            plan_version=plan.version,
            project_status=final.project.status,
            reopened_task_ids=proposal.tasks_to_reopen,
            cancelled_task_ids=proposal.tasks_to_cancel,
            added_task_ids=tuple(item.id for item in proposal.tasks_to_add),
            ready_for_execution=True,
        )

    def _validate_start(
        self, state: ProjectState, request: ChangeReplanningRequest
    ) -> tuple[ChangeRequest, Plan]:
        if state.project.id != request.project_id:
            raise InvalidReplanningState("project identity mismatch")
        if (
            state.project.status is not ProjectStatus.CHANGE_REQUESTED
            or state.project.current_task_id is not None
        ):
            raise InvalidReplanningState(
                "replanning requires CHANGE_REQUESTED at a Task Safe Point"
            )
        changes = tuple(
            item
            for item in state.change_requests
            if item.id == request.change_request_id
        )
        if len(changes) != 1:
            raise InvalidReplanningState(
                "ChangeRequest must exist exactly once"
            )
        change_request = changes[0]
        if (
            change_request.project_id != state.project.id
            or change_request.status is not ChangeRequestStatus.PENDING
        ):
            raise InvalidReplanningState(
                "ChangeRequest is not pending for this Project"
            )
        open_changes = tuple(
            item
            for item in state.change_requests
            if item.status
            in {ChangeRequestStatus.PENDING, ChangeRequestStatus.ANALYZING}
        )
        if open_changes != (change_request,):
            raise InvalidReplanningState(
                "multiple simultaneous ChangeRequests are unsupported"
            )
        active_plans = tuple(
            item
            for item in state.plans
            if item.status is PlanStatus.ACTIVE
        )
        if (
            len(active_plans) != 1
            or active_plans[0].id != state.project.active_plan_id
        ):
            raise InvalidReplanningState(
                "one active Plan is required for replanning"
            )
        return change_request, active_plans[0]

    def _start_replanning(
        self, state: ProjectState, change_request: ChangeRequest
    ) -> ProjectState:
        validate_transition(state.project.status, ProjectStatus.REPLANNING)
        operation_time = self._clock()
        analyzing = replace(
            change_request, status=ChangeRequestStatus.ANALYZING
        )
        updated = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.REPLANNING,
                updated_at=operation_time,
            ),
            change_requests=tuple(
                analyzing if item.id == analyzing.id else item
                for item in state.change_requests
            ),
            events=state.events
            + (
                self._event(
                    state,
                    "replanning.started",
                    change_request.id,
                    operation_time,
                    {},
                ),
            ),
        )
        self._store.save(updated)
        return updated

    def _fail(
        self,
        state: ProjectState,
        change_request: ChangeRequest,
        *,
        event_type: str,
        error_type: str,
    ) -> ProjectState:
        operation_time = self._clock()
        rejected = replace(
            change_request, status=ChangeRequestStatus.REJECTED
        )
        rejected_state = replace(
            state,
            change_requests=tuple(
                rejected if item.id == rejected.id else item
                for item in state.change_requests
            ),
        )
        failed = request_human_action(
            rejected_state,
            category=HumanActionCategory.SUPERVISOR_FAILURE,
            summary="Supervisor change replanning failed validation or execution",
            requested_action="Inspect the replanning failure and choose an explicit resolution",
            risk="The requested change has no trustworthy replacement Plan",
            task_id=None,
            operation_time=operation_time,
            action_id=f"action-{self._event_id_factory()}",
            event_id_factory=self._event_id_factory,
            source_event_types=(event_type,),
            source_metadata={"error_type": error_type},
        )
        self._store.save(failed)
        self._emit(
            failed,
            ProgressEventType.REPLANNING_FAILED,
            "Change replanning requires human review",
            metadata={"error_type": error_type},
        )
        return failed

    def _event(
        self,
        state: ProjectState,
        event_type: str,
        entity_id: str | None,
        timestamp: datetime,
        metadata: dict[str, str],
    ) -> ProjectEvent:
        return ProjectEvent(
            id=self._event_id_factory(),
            project_id=state.project.id,
            event_type=event_type,
            entity_id=entity_id,
            timestamp=timestamp,
            metadata=metadata,
        )

    def _emit(
        self,
        state: ProjectState,
        event_type: ProgressEventType,
        message: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._progress.emit(
            ProgressEvent(
                type=event_type,
                timestamp=self._clock(),
                project_id=state.project.id,
                task_id=None,
                attempt=None,
                message=message,
                metadata={} if metadata is None else metadata,
            )
        )

    @staticmethod
    def _change_request(state: ProjectState, change_id: str) -> ChangeRequest:
        return next(item for item in state.change_requests if item.id == change_id)


__all__ = [
    "ChangeReplanningService",
    "ImpactSupervisor",
    "ProjectStateStore",
]
