"""Deterministic post-completion CHANGE replanning for new revisions."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import subprocess
from typing import Protocol

from code_mule.domain.enums import (
    ChangeRequestStatus,
    HumanActionCategory,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    TaskStatus,
)
from code_mule.domain.models import (
    ChangeRequest,
    ImpactAnalysis,
    Milestone,
    Plan,
    ProjectEvent,
    Requirement,
    Task,
)
from code_mule.domain.state_machine import validate_transition
from code_mule.human import request_human_action
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.recovery import SafePointKind
from code_mule.recovery import ExecutionPhase
from code_mule.recovery.state import with_safe_point
from code_mule.revision import begin_revision
from code_mule.scheduler import SchedulerError, TaskScheduler
from code_mule.state.models import ProjectState
from code_mule.revision import completed_revision
from code_mule.supervisor import (
    ImpactAnalysisRequest,
    ImpactAnalysisResult,
    SupervisorFailureCategory,
    supervisor_failure_metadata,
)

from .contracts import ChangeReplanningOutcome, ChangeReplanningRequest
from .errors import (
    InvalidReplanningState,
    PostCompletionReplanningStage,
    ReplanFailureCode,
    ReplanMaterializationError,
    SupervisorReplanningError,
)


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...
    def save(self, state: ProjectState) -> None: ...


class ImpactSupervisor(Protocol):
    def analyze_change(
        self, request: ImpactAnalysisRequest
    ) -> ImpactAnalysisResult: ...


class PostCompletionReplanner:
    """Reopen one completed revision through impact analysis to Plan vN+1."""

    def __init__(
        self,
        *,
        store: ProjectStateStore,
        supervisor: ImpactSupervisor,
        clock: Callable[[], datetime],
        plan_id_factory: Callable[[], str],
        event_id_factory: Callable[[], str],
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._clock = clock
        self._plan_id_factory = plan_id_factory
        self._event_id_factory = event_id_factory
        self._progress = resilient_progress_sink(progress_sink)
        self._scheduler = TaskScheduler()

    def replan(
        self, request: ChangeReplanningRequest
    ) -> ChangeReplanningOutcome:
        state = self._store.load()
        change_request, base_plan, base_graph_tasks = self._start(
            state, request
        )
        if state.project.status is ProjectStatus.CHANGE_REQUESTED:
            state = self._enter_replanning(state, change_request)
        self._emit(state, ProgressEventType.REPLANNING_STARTED)
        self._emit(state, ProgressEventType.SUPERVISOR_IMPACT_STARTED)
        try:
            proposal = self._supervisor.analyze_change(
                ImpactAnalysisRequest(state, change_request)
            )
        except BaseException as error:
            self._fail(
                state,
                change_request,
                base_plan,
                error,
                stage=PostCompletionReplanningStage.IMPACT_ANALYSIS,
            )
            raise SupervisorReplanningError(
                "Supervisor Impact Analysis failed"
            ) from error
        state = self._store.load()
        if state.project.status is ProjectStatus.CANCELLED:
            raise InvalidReplanningState("replanning was cancelled")
        self._emit(state, ProgressEventType.SUPERVISOR_IMPACT_COMPLETED)
        try:
            self._validate_proposal(
                state, change_request, base_plan, base_graph_tasks, proposal
            )
        except ReplanMaterializationError as error:
            self._fail(
                state,
                change_request,
                base_plan,
                error,
                stage=PostCompletionReplanningStage.PROPOSAL_VALIDATION,
            )
            raise
        try:
            materialized = self._materialize(
                state,
                change_request,
                base_plan,
                base_graph_tasks,
                proposal,
            )
        except ReplanMaterializationError as error:
            self._fail(
                state,
                change_request,
                base_plan,
                error,
                stage=PostCompletionReplanningStage.MATERIALIZATION,
            )
            raise
        plan = next(
            item for item in materialized.plans
            if item.id == materialized.project.active_plan_id
        )
        materialized = with_safe_point(
            materialized,
            SafePointKind.PLAN_MATERIALIZED,
            self._clock(),
        )
        self._store.save(materialized)
        self._emit(
            materialized,
            ProgressEventType.REPLANNING_COMPLETED,
            metadata={
                "previous_plan_id": base_plan.id,
                "plan_id": plan.id,
                "previous_plan_version": str(base_plan.version),
                "plan_version": str(plan.version),
            },
        )
        return ChangeReplanningOutcome(
            project_id=materialized.project.id,
            change_request_id=change_request.id,
            previous_plan_id=base_plan.id,
            plan_id=plan.id,
            previous_plan_version=base_plan.version,
            plan_version=plan.version,
            project_status=materialized.project.status,
            reopened_task_ids=(),
            cancelled_task_ids=(),
            added_task_ids=tuple(item.id for item in proposal.tasks_to_add),
            ready_for_execution=True,
        )

    def _start(
        self, state: ProjectState, request: ChangeReplanningRequest
    ) -> tuple[ChangeRequest, Plan, tuple[Task, ...]]:
        if state.project.id != request.project_id:
            raise InvalidReplanningState("project identity mismatch")
        if state.project.current_task_id is not None:
            raise InvalidReplanningState(
                "reopen requires a completed revision safe boundary"
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
        restarting = state.project.status is ProjectStatus.REPLANNING
        if restarting:
            if change_request.status is not ChangeRequestStatus.ANALYZING:
                raise InvalidReplanningState(
                    "replanning restart requires an ANALYZING ChangeRequest"
                )
        elif (
            state.project.status is not ProjectStatus.CHANGE_REQUESTED
            or change_request.status is not ChangeRequestStatus.PENDING
        ):
            raise InvalidReplanningState(
                "post-completion replanning requires a pending CHANGE_REQUESTED"
            )
        if change_request.requested_revision is None or change_request.base_revision is None:
            raise InvalidReplanningState(
                "post-completion ChangeRequest requires revision fields"
            )
        if (
            change_request.requested_revision
            <= change_request.base_revision
        ):
            raise InvalidReplanningState(
                "requested revision must exceed base revision"
            )
        if any(
            plan.change_request_id == change_request.id
            or plan.revision_number == change_request.requested_revision
            for plan in state.plans
        ):
            raise InvalidReplanningState(
                "ChangeRequest already has a materialized Plan binding"
            )
        if any(
            revision.change_request_id == change_request.id
            or revision.revision_number == change_request.requested_revision
            for revision in state.revisions
        ):
            raise InvalidReplanningState(
                "requested revision already has a materialized record"
            )
        base_plans = tuple(
            plan
            for plan in state.plans
            if plan.id == state.project.active_plan_id
            and plan.id == change_request.base_plan_id
            and plan.version == change_request.base_plan_version
            and plan.status is PlanStatus.COMPLETED
        )
        if len(base_plans) != 1:
            raise InvalidReplanningState(
                "post-completion requires one completed base Plan"
            )
        base_plan = base_plans[0]
        milestones = tuple(
            milestone
            for milestone in state.milestones
            if milestone.plan_id == base_plan.id
            and milestone.id in set(base_plan.milestone_ids)
        )
        base_tasks = tuple(
            task
            for milestone in milestones
            for task in state.tasks
            if task.id in set(milestone.task_ids)
            and task.milestone_id == milestone.id
        )
        if not base_tasks or any(
            task.status is not TaskStatus.COMPLETED for task in base_tasks
        ):
            raise InvalidReplanningState(
                "completed base revision must contain only completed Tasks"
            )
        self._validate_workspace_continuity(state)
        return change_request, base_plan, base_tasks

    @staticmethod
    def _validate_workspace_continuity(state: ProjectState) -> None:
        completed = completed_revision(state)
        expected = (
            None if completed is None else completed.completion_head
        )
        if expected is None or state.project.workspace is None:
            return
        workspace = Path(state.project.workspace)
        if not workspace.is_absolute() or not workspace.is_dir():
            raise InvalidReplanningState("workspace is unavailable")
        try:
            head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            status = subprocess.run(
                (
                    "git",
                    "status",
                    "--short",
                    "--untracked-files=all",
                ),
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InvalidReplanningState(
                "Git workspace could not be validated for the new revision"
            ) from error
        if head.returncode != 0 or head.stdout.strip() != expected:
            raise InvalidReplanningState(
                "workspace HEAD drifted from the completed revision baseline"
            )
        if status.stdout.strip():
            raise InvalidReplanningState(
                "workspace is not clean before the new revision"
            )

    def _enter_replanning(
        self, state: ProjectState, change_request: ChangeRequest
    ) -> ProjectState:
        validate_transition(
            state.project.status, ProjectStatus.REPLANNING
        )
        now = self._clock()
        updated = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.REPLANNING,
                updated_at=now,
            ),
            change_requests=tuple(
                replace(
                    change_request,
                    status=ChangeRequestStatus.ANALYZING,
                )
                if item.id == change_request.id
                else item
                for item in state.change_requests
            ),
            events=state.events
            + (
                ProjectEvent(
                    id=self._event_id_factory(),
                    project_id=state.project.id,
                    event_type="replanning.started",
                    entity_id=change_request.id,
                    timestamp=now,
                    metadata={},
                ),
            ),
        )
        self._store.save(updated)
        return updated

    def _materialize(
        self,
        state: ProjectState,
        change_request: ChangeRequest,
        base_plan: Plan,
        base_tasks: tuple[Task, ...],
        proposal: ImpactAnalysisResult,
    ) -> ProjectState:
        if proposal.change_request_id != change_request.id:
            raise ReplanMaterializationError(
                "proposal targets a different ChangeRequest",
                failure_code=ReplanFailureCode.CHANGE_REQUEST_MISMATCH,
                field_path="change_request_id",
            )
        base_task_ids = tuple(item.id for item in base_tasks)
        plan_id = self._plan_id_factory()
        now = self._clock()
        occupied = {
            state.project.id,
            *(item.id for item in state.requirements),
            *(item.id for item in state.plans),
            *(item.id for item in state.milestones),
            *(item.id for item in state.tasks),
            *(item.id for item in state.change_requests),
        }
        proposed_ids = (
            tuple(item.id for item in proposal.requirements_to_add)
            + tuple(
                item.requirement.id
                for item in proposal.requirements_to_update
            )
            + tuple(item.id for item in proposal.tasks_to_add)
            + tuple(item.id for item in proposal.milestones)
        )
        if (
            plan_id in occupied
            or len(proposed_ids) != len(set(proposed_ids))
            or any(
                entity_id in occupied for entity_id in proposed_ids
            )
        ):
            raise ReplanMaterializationError(
                "proposal or Plan ID collides with history",
                failure_code=ReplanFailureCode.ENTITY_ID_COLLISION,
                field_path="new_entity_ids",
            )

        superseded_requirements = {
            item.supersedes_id for item in proposal.requirements_to_update
        }
        requirements = tuple(
            replace(
                item,
                status=RequirementStatus.SUPERSEDED,
                updated_at=now,
            )
            if item.id in superseded_requirements
            else item
            for item in state.requirements
        )
        requirements += tuple(
            self._requirement(state, change_request, item, now, None)
            for item in proposal.requirements_to_add
        )
        requirements += tuple(
            self._requirement(
                state,
                change_request,
                item.requirement,
                now,
                item.supersedes_id,
            )
            for item in proposal.requirements_to_update
        )

        milestones = tuple(
            Milestone(
                id=item.id,
                plan_id=plan_id,
                title=item.title,
                status="pending",
                task_ids=item.task_ids,
            )
            for item in proposal.milestones
        )
        task_milestones = {
            task_id: milestone.id
            for milestone in milestones
            for task_id in milestone.task_ids
        }
        tasks = tuple(
            Task(
                id=item.id,
                milestone_id=task_milestones[item.id],
                title=item.title,
                description=item.description,
                status=TaskStatus.PENDING,
                dependencies=item.dependencies,
                acceptance_criteria=item.acceptance_criteria,
                execution_attempts=0,
                created_at=now,
                updated_at=now,
                requirement_ids=item.requirement_ids,
                supersedes_task_id=item.supersedes_task_id,
                derived_from_task_ids=item.derived_from_task_ids,
            )
            for item in proposal.tasks_to_add
        )
        plan_requirements = tuple(
            dict.fromkeys(
                tuple(
                    requirement_id
                    for requirement_id in base_plan.requirement_ids
                    if requirement_id not in superseded_requirements
                )
                + tuple(item.id for item in proposal.requirements_to_add)
                + tuple(
                    item.requirement.id
                    for item in proposal.requirements_to_update
                )
            )
        )
        superseded_by_new = {
            item.supersedes_task_id
            for item in proposal.tasks_to_add
            if item.supersedes_task_id is not None
        }
        reused_task_ids = tuple(
            task_id
            for task_id in base_task_ids
            if task_id not in superseded_by_new
        )
        plan = Plan(
            id=plan_id,
            project_id=state.project.id,
            version=max(item.version for item in state.plans) + 1,
            status=PlanStatus.ACTIVE,
            requirement_ids=plan_requirements,
            milestone_ids=tuple(item.id for item in milestones),
            created_at=now,
            base_plan_id=base_plan.id,
            base_plan_version=base_plan.version,
            change_request_id=change_request.id,
            revision_number=change_request.requested_revision,
            reused_task_ids=reused_task_ids,
        )
        applied = replace(
            change_request,
            status=ChangeRequestStatus.APPLIED,
            affected_requirement_ids=tuple(
                dict.fromkeys(
                    proposal.affected_requirement_ids
                    + tuple(item.id for item in proposal.requirements_to_add)
                    + tuple(
                        item.supersedes_id
                        for item in proposal.requirements_to_update
                    )
                )
            ),
        )
        validate_transition(state.project.status, ProjectStatus.RUNNING)
        materialized = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.RUNNING,
                active_plan_id=plan.id,
                current_task_id=None,
                updated_at=now,
            ),
            requirements=requirements,
            plans=state.plans + (plan,),
            milestones=state.milestones + milestones,
            tasks=state.tasks + tasks,
            change_requests=tuple(
                applied if item.id == applied.id else item
                for item in state.change_requests
            ),
            impact_analyses=state.impact_analyses
            + (self._impact(proposal),),
        )
        materialized = begin_revision(
            materialized,
            revision_number=change_request.requested_revision,
            plan_id=plan.id,
            plan_version=plan.version,
            change_request_id=change_request.id,
            base_revision=change_request.base_revision,
            started_at=now,
            baseline_head=None,
        )
        try:
            self._scheduler.validate(materialized)
        except SchedulerError as error:
            raise ReplanMaterializationError(
                "materialized reopened Plan is not schedulable",
                failure_code=ReplanFailureCode.UNSCHEDULABLE_PLAN,
                field_path="replacement_plan",
            ) from error
        return materialized

    def _validate_proposal(
        self,
        state: ProjectState,
        change_request: ChangeRequest,
        base_plan: Plan,
        base_tasks: tuple[Task, ...],
        proposal: ImpactAnalysisResult,
    ) -> None:
        if proposal.tasks_to_reopen or proposal.tasks_to_cancel:
            raise ReplanMaterializationError(
                "completed revision Tasks cannot be reopened or cancelled",
                failure_code=ReplanFailureCode.HISTORICAL_TASK_MUTATION,
                field_path="tasks_to_reopen/tasks_to_cancel",
            )
        if proposal.milestone_ids_reused:
            raise ReplanMaterializationError(
                "reopened Plans must not reuse historical Milestones",
                failure_code=ReplanFailureCode.HISTORICAL_MILESTONE_REUSE,
                field_path="milestone_ids_reused",
            )
        if proposal.affected_in_progress_tasks or proposal.affected_pending_tasks:
            raise ReplanMaterializationError(
                "completed revision has no in-progress or pending Tasks",
                failure_code=ReplanFailureCode.AFFECTED_TASK_CLASSIFICATION,
                field_path=(
                    "affected_in_progress_tasks/affected_pending_tasks"
                ),
            )
        if proposal.dependency_changes or proposal.task_requirement_updates:
            raise ReplanMaterializationError(
                "completed revision Tasks cannot receive in-place updates",
                failure_code=ReplanFailureCode.HISTORICAL_TASK_MUTATION,
                field_path="dependency_changes/task_requirement_updates",
            )
        base_ids = set(base_tasks and tuple(item.id for item in base_tasks))
        unknown = set(proposal.affected_task_ids) - base_ids
        if unknown:
            raise ReplanMaterializationError(
                "affected Task must belong to the completed base Plan",
                failure_code=ReplanFailureCode.AFFECTED_TASK_CLASSIFICATION,
                field_path="affected_task_ids",
            )
        if set(proposal.affected_completed_tasks) != set(
            proposal.affected_task_ids
        ):
            raise ReplanMaterializationError(
                "affected completed Task classification is incomplete",
                failure_code=ReplanFailureCode.AFFECTED_TASK_CLASSIFICATION,
                field_path="affected_completed_tasks",
            )
        new_ids = tuple(item.id for item in proposal.tasks_to_add)
        if len(new_ids) != len(set(new_ids)) or not new_ids:
            raise ReplanMaterializationError(
                "reopened Plan requires at least one new executable Task",
                failure_code=ReplanFailureCode.NEW_TASK_REQUIRED,
                field_path="tasks_to_add",
            )
        milestone_task_ids = tuple(
            task_id
            for milestone in proposal.milestones
            for task_id in milestone.task_ids
        )
        if (
            len(milestone_task_ids) != len(set(milestone_task_ids))
            or set(milestone_task_ids) != set(new_ids)
        ):
            raise ReplanMaterializationError(
                "reopened Plan milestones must own exactly the new Tasks",
                failure_code=ReplanFailureCode.NEW_MILESTONE_TASK_COVERAGE,
                field_path="milestones[*].task_ids",
            )
        for task in proposal.tasks_to_add:
            if (
                task.supersedes_task_id is not None
                and task.supersedes_task_id in task.derived_from_task_ids
            ):
                raise ReplanMaterializationError(
                    "Task replacement and derived lineage must not repeat "
                    "the same historical Task",
                    failure_code=ReplanFailureCode.CONFLICTING_TASK_LINEAGE,
                    field_path="tasks_to_add[*].derived_from_task_ids",
                )
            if task.supersedes_task_id is not None and (
                task.supersedes_task_id not in base_ids
            ):
                raise ReplanMaterializationError(
                    f"Task {task.id} supersedes an unknown historical Task",
                    failure_code=ReplanFailureCode.UNKNOWN_TASK_LINEAGE,
                    field_path="tasks_to_add[*].supersedes_task_id",
                )
            if task.derived_from_task_ids and not set(
                task.derived_from_task_ids
            ).issubset(base_ids):
                raise ReplanMaterializationError(
                    f"Task {task.id} derives from an unknown historical Task",
                    failure_code=ReplanFailureCode.UNKNOWN_TASK_LINEAGE,
                    field_path="tasks_to_add[*].derived_from_task_ids",
                )
        self._acyclic(proposal)

    @staticmethod
    def _acyclic(proposal: ImpactAnalysisResult) -> None:
        ids = {item.id for item in proposal.tasks_to_add}
        graph = {
            item.id: tuple(
                dependency
                for dependency in item.dependencies
                if dependency in ids
            )
            for item in proposal.tasks_to_add
        }
        states: dict[str, str] = {}

        def visit(task_id: str) -> None:
            state = states.get(task_id)
            if state == "visiting":
                raise ReplanMaterializationError(
                    "new Task dependency cycle",
                    failure_code=ReplanFailureCode.NEW_TASK_DEPENDENCY_CYCLE,
                    field_path="tasks_to_add[*].dependencies",
                )
            if state == "visited":
                return
            states[task_id] = "visiting"
            for dependency in graph[task_id]:
                visit(dependency)
            states[task_id] = "visited"

        for task_id in ids:
            visit(task_id)

    @staticmethod
    def _requirement(
        state: ProjectState,
        change_request: ChangeRequest,
        item,
        operation_time: datetime,
        supersedes_id: str | None,
    ) -> Requirement:
        return Requirement(
            id=item.id,
            project_id=state.project.id,
            title=item.title,
            description=item.description,
            status=RequirementStatus.ACTIVE,
            priority=item.priority,
            acceptance_criteria=item.acceptance_criteria,
            introduced_by=change_request.id,
            created_at=operation_time,
            updated_at=operation_time,
            supersedes_id=supersedes_id,
        )

    @staticmethod
    def _impact(proposal: ImpactAnalysisResult) -> ImpactAnalysis:
        return ImpactAnalysis(
            change_request_id=proposal.change_request_id,
            architecture_impact=proposal.architecture_impact,
            affected_components=proposal.affected_components,
            affected_completed_tasks=proposal.affected_completed_tasks,
            affected_in_progress_tasks=(),
            affected_pending_tasks=(),
            tasks_to_add=tuple(item.id for item in proposal.tasks_to_add),
            tasks_to_reopen=(),
            tasks_to_cancel=(),
            recommendation=proposal.recommendation,
            summary=proposal.summary,
            affected_requirement_ids=proposal.affected_requirement_ids,
            affected_task_ids=proposal.affected_task_ids,
            requirements_to_add=tuple(
                item.id for item in proposal.requirements_to_add
            ),
            requirements_to_update=tuple(
                f"{item.supersedes_id}->{item.requirement.id}"
                for item in proposal.requirements_to_update
            ),
            milestone_ids=tuple(item.id for item in proposal.milestones),
            dependency_changes=(),
            risks=proposal.risks,
            rationale=proposal.rationale,
        )

    def _fail(
        self,
        state: ProjectState,
        change_request: ChangeRequest,
        base_plan: Plan,
        error: BaseException,
        *,
        stage: PostCompletionReplanningStage,
    ) -> None:
        state = self._store.load()
        now = self._clock()
        rejected = replace(
            change_request,
            status=ChangeRequestStatus.REJECTED,
        )
        metadata = supervisor_failure_metadata(
            error,
            category=(
                SupervisorFailureCategory.DETERMINISTIC_VALIDATION_FAILURE
                if isinstance(error, (ValueError, ReplanMaterializationError))
                else None
            ),
        )
        supervisor_operation = metadata.pop("operation", None)
        metadata.update(
            {
                "operation": "post_completion_replanning",
                "stage": stage.value,
                "change_request_id": change_request.id,
                "base_revision": str(change_request.base_revision),
                "requested_revision": str(change_request.requested_revision),
                "base_plan_id": base_plan.id,
                "base_plan_version": str(base_plan.version),
                "target_plan_version": str(base_plan.version + 1),
                "plan_materialized": "false",
                "revision_materialized": "false",
                "worker_started": "false",
            }
        )
        if supervisor_operation is not None:
            metadata["supervisor_operation"] = supervisor_operation
        if isinstance(error, ReplanMaterializationError):
            metadata["validation_code"] = error.failure_code.value
            if error.field_path is not None:
                metadata["field_path"] = error.field_path
        failed = replace(
            state,
            change_requests=tuple(
                rejected if item.id == rejected.id else item
                for item in state.change_requests
            ),
        )
        failed = request_human_action(
            failed,
            category=HumanActionCategory.SUPERVISOR_FAILURE,
            summary="Supervisor post-completion replanning failed",
            requested_action=(
                "Inspect the failure and choose an explicit resolution"
            ),
            risk="The new revision has no trustworthy replacement Plan",
            task_id=None,
            operation_time=now,
            action_id=f"action-{self._event_id_factory()}",
            event_id_factory=self._event_id_factory,
            source_event_types=("replanning.failed",),
            source_metadata=metadata,
            phase=ExecutionPhase.REPLANNING,
        )
        self._store.save(failed)

    def _emit(
        self,
        state: ProjectState,
        event_type: ProgressEventType,
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
                message=event_type.value.replace("_", " ").title(),
                metadata={} if metadata is None else metadata,
            )
        )


__all__ = ["PostCompletionReplanner"]
