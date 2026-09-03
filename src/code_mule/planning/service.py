"""Deterministic orchestration of initial Supervisor planning."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from code_mule.domain.enums import HumanActionCategory, PlanStatus, ProjectStatus
from code_mule.domain.models import ProjectEvent
from code_mule.domain.state_machine import validate_transition
from code_mule.human import request_human_action
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.state.models import ProjectState
from code_mule.supervisor.contracts import PlanProposal, PlanRequest
from code_mule.supervisor import (
    SupervisorFailureCategory,
    supervisor_failure_metadata,
)

from .contracts import ProjectPlanningOutcome, ProjectPlanningRequest
from .errors import (
    InvalidPlanProposal,
    PlanMaterializationError,
    ProjectPlanningStateError,
    SupervisorPlanningError,
    UnknownProposalReference,
)
from .materialization import PlanMaterializer
from .validation import PlanProposalValidator


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...

    def save(self, state: ProjectState) -> None: ...


class SupervisorPlanner(Protocol):
    def plan(self, request: PlanRequest) -> PlanProposal: ...


class ProjectPlanningService:
    """Persist IDLE→PLANNING, validate one proposal, and atomically materialize."""

    def __init__(
        self,
        *,
        store: ProjectStateStore,
        supervisor: SupervisorPlanner,
        clock: Callable[[], datetime],
        plan_id_factory: Callable[[], str],
        event_id_factory: Callable[[], str],
        progress_sink: ProgressSink | None = None,
        validator: PlanProposalValidator | None = None,
        materializer: PlanMaterializer | None = None,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._clock = clock
        self._plan_id_factory = plan_id_factory
        self._event_id_factory = event_id_factory
        self._progress = resilient_progress_sink(progress_sink)
        self._validator = validator or PlanProposalValidator()
        self._materializer = materializer or PlanMaterializer(self._validator)

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def plan(self, request: ProjectPlanningRequest) -> ProjectPlanningOutcome:
        initial = self._store.load()
        self._validate_initial_state(initial, request)
        planning = self._start_planning(initial, request.objective)
        self._emit(
            planning,
            ProgressEventType.PLANNING_STARTED,
            "Project planning started",
            metadata={"project_status": ProjectStatus.PLANNING.value},
        )
        self._emit(
            planning,
            ProgressEventType.SUPERVISOR_PLAN_STARTED,
            "Supervisor planning",
        )
        try:
            proposal = self._supervisor.plan(
                PlanRequest(project_state=planning, objective=request.objective)
            )
        except Exception as error:
            self._fail_planning(
                planning,
                event_type="planning.failed",
                failure_metadata=supervisor_failure_metadata(error),
            )
            raise SupervisorPlanningError("Supervisor PLAN failed") from error

        self._emit(
            planning,
            ProgressEventType.SUPERVISOR_PLAN_COMPLETED,
            "Supervisor plan proposal received",
        )
        try:
            self._validator.validate(planning, proposal)
        except InvalidPlanProposal as error:
            self._fail_planning(
                planning,
                event_type="planning.proposal_rejected",
                failure_metadata=supervisor_failure_metadata(
                    error,
                    category=(
                        SupervisorFailureCategory.INVALID_BUSINESS_REFERENCE
                        if isinstance(error, UnknownProposalReference)
                        else SupervisorFailureCategory.DETERMINISTIC_VALIDATION_FAILURE
                    ),
                ),
            )
            raise

        self._emit(
            planning,
            ProgressEventType.PLANNING_MATERIALIZING,
            "Materializing validated plan",
        )
        plan_id = self._plan_id_factory()
        operation_time = self._clock()
        try:
            materialized = self._materializer.materialize(
                planning,
                proposal,
                plan_id=plan_id,
                operation_time=operation_time,
            )
        except PlanMaterializationError as error:
            self._fail_planning(
                planning,
                event_type="planning.failed",
                failure_metadata=supervisor_failure_metadata(
                    error,
                    category=(
                        SupervisorFailureCategory.DETERMINISTIC_VALIDATION_FAILURE
                    ),
                ),
            )
            raise

        plan = next(item for item in materialized.plans if item.id == plan_id)
        completed_event = self._event(
            materialized,
            "planning.completed",
            plan_id,
            operation_time,
            {"plan_version": str(plan.version)},
        )
        materialized_event = self._event(
            materialized,
            "plan.materialized",
            plan_id,
            operation_time,
            {
                "requirements": str(len(plan.requirement_ids)),
                "milestones": str(len(plan.milestone_ids)),
                "tasks": str(len(proposal.tasks)),
            },
        )
        final_state = replace(
            materialized,
            events=materialized.events + (completed_event, materialized_event),
        )
        self._store.save(final_state)
        self._emit(
            final_state,
            ProgressEventType.PLANNING_COMPLETED,
            f"Plan v{plan.version} created with {len(proposal.tasks)} tasks",
            metadata={
                "project_status": ProjectStatus.RUNNING.value,
                "plan_id": plan.id,
                "plan_version": str(plan.version),
                "requirement_count": str(len(plan.requirement_ids)),
                "total_tasks": str(len(proposal.tasks)),
            },
        )
        return ProjectPlanningOutcome(
            project_id=final_state.project.id,
            plan_id=plan.id,
            plan_version=plan.version,
            requirement_ids=plan.requirement_ids,
            milestone_ids=plan.milestone_ids,
            task_ids=tuple(item.id for item in proposal.tasks),
            project_status=final_state.project.status,
            ready_for_execution=True,
        )

    def _validate_initial_state(
        self, state: ProjectState, request: ProjectPlanningRequest
    ) -> None:
        if state.project.id != request.project_id:
            raise ProjectPlanningStateError("project identity mismatch")
        if (
            state.project.status is not ProjectStatus.IDLE
            or state.project.active_plan_id is not None
            or state.project.current_task_id is not None
            or any(plan.status is PlanStatus.ACTIVE for plan in state.plans)
        ):
            raise ProjectPlanningStateError(
                "initial planning requires an unbound IDLE Project"
            )

    def _start_planning(self, state: ProjectState, objective: str) -> ProjectState:
        validate_transition(state.project.status, ProjectStatus.PLANNING)
        operation_time = self._clock()
        updated = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.PLANNING,
                objective=objective,
                updated_at=operation_time,
            ),
            events=state.events
            + (
                self._event(
                    state,
                    "planning.started",
                    state.project.id,
                    operation_time,
                    {},
                ),
            ),
        )
        self._store.save(updated)
        return updated

    def _fail_planning(
        self,
        state: ProjectState,
        *,
        event_type: str,
        failure_metadata: dict[str, str],
    ) -> ProjectState:
        operation_time = self._clock()
        failed = request_human_action(
            state,
            category=HumanActionCategory.SUPERVISOR_FAILURE,
            summary="Supervisor planning failed validation or execution",
            requested_action="Inspect the planning failure and choose an explicit resolution",
            risk="No active Plan can be trusted until the failure is resolved",
            task_id=None,
            operation_time=operation_time,
            action_id=f"action-{self._event_id_factory()}",
            event_id_factory=self._event_id_factory,
            source_event_types=(event_type,),
            source_metadata=failure_metadata,
        )
        self._store.save(failed)
        self._emit(
            failed,
            ProgressEventType.PLANNING_FAILED,
            "Project planning requires human review",
            metadata=failure_metadata,
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


__all__ = ["ProjectPlanningService", "ProjectStateStore", "SupervisorPlanner"]
