"""Explicit JSON-compatible serialization for project-state snapshots."""

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

from code_mule.domain.enums import (
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import (
    ChangeRequest,
    Decision,
    ExecutionReport,
    ImpactAnalysis,
    HumanAction,
    HumanResolution,
    Milestone,
    Plan,
    Project,
    ProjectEvent,
    QualityStatus,
    Requirement,
    Task,
    WorkerInputDetails,
)
from code_mule.execution.contracts import ExecutionLease, ExecutionLeaseStatus
from code_mule.git_delivery.contracts import GitBaseline, GitChangeSet, GitCommitResult
from code_mule.project_verification.contracts import (
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCheck,
    ProjectVerificationCommand,
    ProjectVerificationResult,
    ProjectVerificationSpec,
    ProjectVerificationStatus,
)

from .models import ProjectState


CURRENT_SCHEMA_VERSION = 9


class UnsupportedStateSchema(ValueError):
    """Raised when a state payload has no supported schema version."""


class InvalidProjectState(ValueError):
    """Raised when a state payload cannot form a complete valid snapshot."""


_T = TypeVar("_T")


def _expect_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InvalidProjectState(f"{context} must be an object with string keys")
    return cast(dict[str, object], value)


def _expect_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidProjectState(f"{context} must be a list")
    return cast(list[object], value)


def _expect_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise InvalidProjectState(f"{context} must be a string")
    return value


def _expect_optional_str(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, context)


def _expect_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise InvalidProjectState(f"{context} must be an integer")
    return cast(int, value)


def _expect_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise InvalidProjectState(f"{context} must be a boolean")
    return cast(bool, value)


def _expect_number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidProjectState(f"{context} must be a number")
    return float(value)


def _field(payload: dict[str, object], name: str, context: str) -> object:
    if name not in payload:
        raise InvalidProjectState(f"{context} is missing required field {name!r}")
    return payload[name]


def _datetime(value: object, context: str) -> datetime:
    raw = _expect_str(value, context)
    try:
        return datetime.fromisoformat(raw)
    except ValueError as error:
        raise InvalidProjectState(f"{context} is not a valid ISO 8601 datetime") from error


def _tuple_of(
    value: object,
    context: str,
    deserialize_item: Callable[[object], _T],
) -> tuple[_T, ...]:
    return tuple(deserialize_item(item) for item in _expect_list(value, context))


def _strings(value: object, context: str) -> tuple[str, ...]:
    return _tuple_of(value, context, lambda item: _expect_str(item, context))


def _project_to_payload(project: Project) -> dict[str, object]:
    return {
        "id": project.id,
        "name": project.name,
        "status": project.status.value,
        "active_plan_id": project.active_plan_id,
        "current_task_id": project.current_task_id,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "workspace": project.workspace,
        "objective": project.objective,
    }


def _requirement_to_payload(requirement: Requirement) -> dict[str, object]:
    return {
        "id": requirement.id,
        "project_id": requirement.project_id,
        "title": requirement.title,
        "description": requirement.description,
        "status": requirement.status.value,
        "priority": requirement.priority,
        "acceptance_criteria": list(requirement.acceptance_criteria),
        "introduced_by": requirement.introduced_by,
        "created_at": requirement.created_at.isoformat(),
        "updated_at": requirement.updated_at.isoformat(),
        "supersedes_id": requirement.supersedes_id,
    }


def _plan_to_payload(plan: Plan) -> dict[str, object]:
    return {
        "id": plan.id,
        "project_id": plan.project_id,
        "version": plan.version,
        "status": plan.status.value,
        "requirement_ids": list(plan.requirement_ids),
        "milestone_ids": list(plan.milestone_ids),
        "created_at": plan.created_at.isoformat(),
    }


def _milestone_to_payload(milestone: Milestone) -> dict[str, object]:
    return {
        "id": milestone.id,
        "plan_id": milestone.plan_id,
        "title": milestone.title,
        "status": milestone.status,
        "task_ids": list(milestone.task_ids),
    }


def _task_to_payload(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "milestone_id": task.milestone_id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "dependencies": list(task.dependencies),
        "acceptance_criteria": list(task.acceptance_criteria),
        "requirement_ids": list(task.requirement_ids),
        "execution_attempts": task.execution_attempts,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _change_request_to_payload(change_request: ChangeRequest) -> dict[str, object]:
    return {
        "id": change_request.id,
        "project_id": change_request.project_id,
        "description": change_request.description,
        "status": change_request.status.value,
        "affected_requirement_ids": list(change_request.affected_requirement_ids),
        "created_by": change_request.created_by,
        "created_at": change_request.created_at.isoformat(),
    }


def _impact_analysis_to_payload(impact: ImpactAnalysis) -> dict[str, object]:
    return {
        "change_request_id": impact.change_request_id,
        "architecture_impact": impact.architecture_impact,
        "affected_components": list(impact.affected_components),
        "affected_completed_tasks": list(impact.affected_completed_tasks),
        "affected_in_progress_tasks": list(impact.affected_in_progress_tasks),
        "affected_pending_tasks": list(impact.affected_pending_tasks),
        "tasks_to_add": list(impact.tasks_to_add),
        "tasks_to_reopen": list(impact.tasks_to_reopen),
        "tasks_to_cancel": list(impact.tasks_to_cancel),
        "recommendation": impact.recommendation,
        "summary": impact.summary,
        "affected_requirement_ids": list(impact.affected_requirement_ids),
        "affected_task_ids": list(impact.affected_task_ids),
        "requirements_to_add": list(impact.requirements_to_add),
        "requirements_to_update": list(impact.requirements_to_update),
        "milestone_ids": list(impact.milestone_ids),
        "dependency_changes": list(impact.dependency_changes),
        "risks": list(impact.risks),
        "rationale": impact.rationale,
    }


def _decision_to_payload(decision: Decision) -> dict[str, object]:
    return {
        "id": decision.id,
        "task_id": decision.task_id,
        "type": decision.type.value,
        "rationale": decision.rationale,
        "created_at": decision.created_at.isoformat(),
    }


def _execution_report_to_payload(report: ExecutionReport) -> dict[str, object]:
    return {
        "id": report.id,
        "task_id": report.task_id,
        "attempt": report.attempt,
        "status": report.status,
        "files_changed": list(report.files_changed),
        "tests": list(report.tests),
        "static_checks": list(report.static_checks),
        "git_state": report.git_state,
        "issues": list(report.issues),
        "human_action_required": report.human_action_required,
        "summary": report.summary,
        "created_at": report.created_at.isoformat(),
    }


def _quality_status_to_payload(quality: QualityStatus) -> dict[str, object]:
    return {
        "tests": quality.tests,
        "lint": quality.lint,
        "type_check": quality.type_check,
        "build": quality.build,
        "repository_clean": quality.repository_clean,
    }


def _event_to_payload(event: ProjectEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "project_id": event.project_id,
        "event_type": event.event_type,
        "entity_id": event.entity_id,
        "timestamp": event.timestamp.isoformat(),
        "metadata": dict(event.metadata),
    }


def _human_action_to_payload(action: HumanAction) -> dict[str, object]:
    return {
        "id": action.id,
        "project_id": action.project_id,
        "task_id": action.task_id,
        "category": action.category.value,
        "summary": action.summary,
        "requested_action": action.requested_action,
        "risk": action.risk,
        "status": action.status.value,
        "created_at": action.created_at.isoformat(),
        "resolved_at": (
            None if action.resolved_at is None else action.resolved_at.isoformat()
        ),
        "worker_input": (
            None
            if action.worker_input is None
            else _worker_input_to_payload(action.worker_input)
        ),
    }


def _worker_input_to_payload(details: WorkerInputDetails) -> dict[str, object]:
    return {
        "request_method": details.request_method,
        "request_id": details.request_id,
        "question": details.question,
        "choices": list(details.choices),
        "worker_attempt": details.worker_attempt,
        "baseline_head": details.baseline_head,
        "partial_paths": list(details.partial_paths),
        "answer": details.answer,
    }


def _human_resolution_to_payload(resolution: HumanResolution) -> dict[str, object]:
    return {
        "id": resolution.id,
        "action_id": resolution.action_id,
        "project_id": resolution.project_id,
        "strategy": resolution.strategy.value,
        "summary": resolution.summary,
        "created_at": resolution.created_at.isoformat(),
    }


def _execution_lease_to_payload(lease: ExecutionLease) -> dict[str, object]:
    return {
        "id": lease.id,
        "project_id": lease.project_id,
        "owner_id": lease.owner_id,
        "pid": lease.pid,
        "acquired_at": lease.acquired_at.isoformat(),
        "heartbeat_at": lease.heartbeat_at.isoformat(),
        "status": lease.status.value,
        "current_task_id": lease.current_task_id,
        "codex_thread_id": lease.codex_thread_id,
        "attempt": lease.attempt,
    }


def _git_baseline_to_payload(baseline: GitBaseline) -> dict[str, object]:
    return {
        "task_id": baseline.task_id,
        "repository_root": baseline.repository_root,
        "baseline_head": baseline.baseline_head,
        "status_entries": list(baseline.status_entries),
    }


def _git_change_set_to_payload(change_set: GitChangeSet) -> dict[str, object]:
    return {
        "task_id": change_set.task_id,
        "repository_root": change_set.repository_root,
        "baseline_head": change_set.baseline_head,
        "changed_paths": list(change_set.changed_paths),
        "untracked_paths": list(change_set.untracked_paths),
        "staged_paths": list(change_set.staged_paths),
    }


def _git_commit_result_to_payload(result: GitCommitResult) -> dict[str, object]:
    return {
        "task_id": result.task_id,
        "repository_root": result.repository_root,
        "baseline_head": result.baseline_head,
        "commit_sha": result.commit_sha,
        "commit_message": result.commit_message,
        "changed_paths": list(result.changed_paths),
        "staged_paths": list(result.staged_paths),
        "committed_at": result.committed_at.isoformat(),
    }


def _verification_command_to_payload(
    command: ProjectVerificationCommand,
) -> dict[str, object]:
    return {
        "name": command.name,
        "category": command.category.value,
        "command": list(command.command),
        "required": command.required,
        "timeout_seconds": command.timeout_seconds,
        "network_allowed": command.network_allowed,
    }


def _verification_spec_to_payload(
    spec: ProjectVerificationSpec,
) -> dict[str, object]:
    return {
        "project_id": spec.project_id,
        "commands": [_verification_command_to_payload(item) for item in spec.commands],
    }


def _verification_check_to_payload(
    check: ProjectVerificationCheck,
) -> dict[str, object]:
    return {
        "name": check.name,
        "category": check.category.value,
        "command": list(check.command),
        "status": check.status.value,
        "exit_code": check.exit_code,
        "safe_summary": check.safe_summary,
        "required": check.required,
    }


def _verification_result_to_payload(
    result: ProjectVerificationResult,
) -> dict[str, object]:
    return {
        "id": result.id,
        "project_id": result.project_id,
        "plan_id": result.plan_id,
        "expected_head": result.expected_head,
        "verified_head": result.verified_head,
        "checks": [_verification_check_to_payload(item) for item in result.checks],
        "started_at": result.started_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
        "final_review_decision": (
            None
            if result.final_review_decision is None
            else result.final_review_decision.value
        ),
        "final_review_summary": result.final_review_summary,
    }


def serialize_project_state(state: ProjectState) -> dict[str, object]:
    """Convert a complete snapshot to a JSON-compatible object."""

    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "project": _project_to_payload(state.project),
        "requirements": [_requirement_to_payload(item) for item in state.requirements],
        "plans": [_plan_to_payload(item) for item in state.plans],
        "milestones": [_milestone_to_payload(item) for item in state.milestones],
        "tasks": [_task_to_payload(item) for item in state.tasks],
        "change_requests": [
            _change_request_to_payload(item) for item in state.change_requests
        ],
        "impact_analyses": [
            _impact_analysis_to_payload(item) for item in state.impact_analyses
        ],
        "decisions": [_decision_to_payload(item) for item in state.decisions],
        "execution_reports": [
            _execution_report_to_payload(item) for item in state.execution_reports
        ],
        "quality_status": (
            None
            if state.quality_status is None
            else _quality_status_to_payload(state.quality_status)
        ),
        "events": [_event_to_payload(item) for item in state.events],
        "human_actions": [
            _human_action_to_payload(item) for item in state.human_actions
        ],
        "human_resolutions": [
            _human_resolution_to_payload(item) for item in state.human_resolutions
        ],
        "execution_leases": [
            _execution_lease_to_payload(item) for item in state.execution_leases
        ],
        "git_baselines": [
            _git_baseline_to_payload(item) for item in state.git_baselines
        ],
        "git_change_sets": [
            _git_change_set_to_payload(item) for item in state.git_change_sets
        ],
        "git_commit_results": [
            _git_commit_result_to_payload(item) for item in state.git_commit_results
        ],
        "project_verification_spec": (
            None
            if state.project_verification_spec is None
            else _verification_spec_to_payload(state.project_verification_spec)
        ),
        "project_verification_results": [
            _verification_result_to_payload(item)
            for item in state.project_verification_results
        ],
    }


def _project_from_payload(value: object) -> Project:
    payload = _expect_object(value, "project")
    return Project(
        id=_expect_str(_field(payload, "id", "project"), "project.id"),
        name=_expect_str(_field(payload, "name", "project"), "project.name"),
        status=ProjectStatus(
            _expect_str(_field(payload, "status", "project"), "project.status")
        ),
        active_plan_id=_expect_optional_str(
            _field(payload, "active_plan_id", "project"), "project.active_plan_id"
        ),
        current_task_id=_expect_optional_str(
            _field(payload, "current_task_id", "project"), "project.current_task_id"
        ),
        created_at=_datetime(
            _field(payload, "created_at", "project"), "project.created_at"
        ),
        updated_at=_datetime(
            _field(payload, "updated_at", "project"), "project.updated_at"
        ),
        workspace=_expect_optional_str(
            _field(payload, "workspace", "project"), "project.workspace"
        ),
        objective=_expect_optional_str(
            _field(payload, "objective", "project"), "project.objective"
        ),
    )


def _requirement_from_payload(value: object) -> Requirement:
    payload = _expect_object(value, "requirement")
    return Requirement(
        id=_expect_str(_field(payload, "id", "requirement"), "requirement.id"),
        project_id=_expect_str(
            _field(payload, "project_id", "requirement"), "requirement.project_id"
        ),
        title=_expect_str(
            _field(payload, "title", "requirement"), "requirement.title"
        ),
        description=_expect_str(
            _field(payload, "description", "requirement"), "requirement.description"
        ),
        status=RequirementStatus(
            _expect_str(
                _field(payload, "status", "requirement"), "requirement.status"
            )
        ),
        priority=_expect_str(
            _field(payload, "priority", "requirement"), "requirement.priority"
        ),
        acceptance_criteria=_strings(
            _field(payload, "acceptance_criteria", "requirement"),
            "requirement.acceptance_criteria",
        ),
        introduced_by=_expect_str(
            _field(payload, "introduced_by", "requirement"),
            "requirement.introduced_by",
        ),
        created_at=_datetime(
            _field(payload, "created_at", "requirement"), "requirement.created_at"
        ),
        updated_at=_datetime(
            _field(payload, "updated_at", "requirement"), "requirement.updated_at"
        ),
        supersedes_id=_expect_optional_str(
            _field(payload, "supersedes_id", "requirement"),
            "requirement.supersedes_id",
        ),
    )


def _plan_from_payload(value: object) -> Plan:
    payload = _expect_object(value, "plan")
    return Plan(
        id=_expect_str(_field(payload, "id", "plan"), "plan.id"),
        project_id=_expect_str(
            _field(payload, "project_id", "plan"), "plan.project_id"
        ),
        version=_expect_int(_field(payload, "version", "plan"), "plan.version"),
        status=PlanStatus(
            _expect_str(_field(payload, "status", "plan"), "plan.status")
        ),
        requirement_ids=_strings(
            _field(payload, "requirement_ids", "plan"), "plan.requirement_ids"
        ),
        milestone_ids=_strings(
            _field(payload, "milestone_ids", "plan"), "plan.milestone_ids"
        ),
        created_at=_datetime(
            _field(payload, "created_at", "plan"), "plan.created_at"
        ),
    )


def _milestone_from_payload(value: object) -> Milestone:
    payload = _expect_object(value, "milestone")
    return Milestone(
        id=_expect_str(_field(payload, "id", "milestone"), "milestone.id"),
        plan_id=_expect_str(
            _field(payload, "plan_id", "milestone"), "milestone.plan_id"
        ),
        title=_expect_str(
            _field(payload, "title", "milestone"), "milestone.title"
        ),
        status=_expect_str(
            _field(payload, "status", "milestone"), "milestone.status"
        ),
        task_ids=_strings(
            _field(payload, "task_ids", "milestone"), "milestone.task_ids"
        ),
    )


def _task_from_payload(value: object) -> Task:
    payload = _expect_object(value, "task")
    return Task(
        id=_expect_str(_field(payload, "id", "task"), "task.id"),
        milestone_id=_expect_str(
            _field(payload, "milestone_id", "task"), "task.milestone_id"
        ),
        title=_expect_str(_field(payload, "title", "task"), "task.title"),
        description=_expect_str(
            _field(payload, "description", "task"), "task.description"
        ),
        status=TaskStatus(
            _expect_str(_field(payload, "status", "task"), "task.status")
        ),
        dependencies=_strings(
            _field(payload, "dependencies", "task"), "task.dependencies"
        ),
        acceptance_criteria=_strings(
            _field(payload, "acceptance_criteria", "task"),
            "task.acceptance_criteria",
        ),
        execution_attempts=_expect_int(
            _field(payload, "execution_attempts", "task"),
            "task.execution_attempts",
        ),
        created_at=_datetime(
            _field(payload, "created_at", "task"), "task.created_at"
        ),
        updated_at=_datetime(
            _field(payload, "updated_at", "task"), "task.updated_at"
        ),
        requirement_ids=_strings(
            _field(payload, "requirement_ids", "task"),
            "task.requirement_ids",
        ),
    )


def _migrate_v1_to_v2(root: dict[str, object]) -> dict[str, object]:
    """Return a v2 snapshot without mutating the historical v1 payload."""

    migrated_tasks: list[dict[str, object]] = []
    for item in _expect_list(_field(root, "tasks", "project state"), "tasks"):
        task = dict(_expect_object(item, "task"))
        task["requirement_ids"] = []
        migrated_tasks.append(task)
    migrated = dict(root)
    migrated["schema_version"] = 2
    migrated["tasks"] = migrated_tasks
    return migrated


def _migrate_v2_to_v3(root: dict[str, object]) -> dict[str, object]:
    """Add explicit Requirement replacement and rich Impact metadata."""

    migrated_requirements: list[dict[str, object]] = []
    for item in _expect_list(
        _field(root, "requirements", "project state"), "requirements"
    ):
        requirement = dict(_expect_object(item, "requirement"))
        requirement["supersedes_id"] = None
        migrated_requirements.append(requirement)

    migrated_impacts: list[dict[str, object]] = []
    for item in _expect_list(
        _field(root, "impact_analyses", "project state"), "impact_analyses"
    ):
        impact = dict(_expect_object(item, "impact_analysis"))
        impact.update(
            {
                "summary": "",
                "affected_requirement_ids": [],
                "affected_task_ids": [],
                "requirements_to_add": [],
                "requirements_to_update": [],
                "milestone_ids": [],
                "dependency_changes": [],
                "risks": [],
                "rationale": "",
            }
        )
        migrated_impacts.append(impact)

    migrated = dict(root)
    migrated["schema_version"] = 3
    migrated["requirements"] = migrated_requirements
    migrated["impact_analyses"] = migrated_impacts
    return migrated


def _migrate_v3_to_v4(root: dict[str, object]) -> dict[str, object]:
    """Add the persisted Worker workspace required by the Boss CLI."""

    project = dict(
        _expect_object(_field(root, "project", "project state"), "project")
    )
    project["workspace"] = None
    migrated = dict(root)
    migrated["schema_version"] = 4
    migrated["project"] = project
    return migrated


def _migrate_v4_to_v5(root: dict[str, object]) -> dict[str, object]:
    """Add durable typed HumanAction and HumanResolution collections."""

    migrated = dict(root)
    migrated["schema_version"] = 5
    migrated["human_actions"] = []
    migrated["human_resolutions"] = []
    return migrated


def _migrate_v5_to_v6(root: dict[str, object]) -> dict[str, object]:
    """Add durable local execution ownership history."""

    migrated = dict(root)
    migrated["schema_version"] = 6
    migrated["execution_leases"] = []
    return migrated


def _migrate_v6_to_v7(root: dict[str, object]) -> dict[str, object]:
    """Add durable per-Task Git delivery evidence."""

    migrated = dict(root)
    migrated["schema_version"] = 7
    migrated["git_baselines"] = []
    migrated["git_change_sets"] = []
    migrated["git_commit_results"] = []
    return migrated


def _migrate_v7_to_v8(root: dict[str, object]) -> dict[str, object]:
    """Add deterministic project verification configuration and evidence."""

    project = dict(
        _expect_object(_field(root, "project", "project state"), "project")
    )
    project["objective"] = None
    migrated = dict(root)
    migrated["schema_version"] = 8
    migrated["project"] = project
    migrated["project_verification_spec"] = None
    migrated["project_verification_results"] = []
    return migrated


def _migrate_v8_to_v9(root: dict[str, object]) -> dict[str, object]:
    """Add bounded Worker input details to durable HumanActions."""

    actions: list[dict[str, object]] = []
    for item in _expect_list(
        _field(root, "human_actions", "project state"), "human_actions"
    ):
        action = dict(_expect_object(item, "human_action"))
        action["worker_input"] = None
        actions.append(action)
    migrated = dict(root)
    migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    migrated["human_actions"] = actions
    return migrated


def _change_request_from_payload(value: object) -> ChangeRequest:
    payload = _expect_object(value, "change_request")
    return ChangeRequest(
        id=_expect_str(_field(payload, "id", "change_request"), "change_request.id"),
        project_id=_expect_str(
            _field(payload, "project_id", "change_request"),
            "change_request.project_id",
        ),
        description=_expect_str(
            _field(payload, "description", "change_request"),
            "change_request.description",
        ),
        status=ChangeRequestStatus(
            _expect_str(
                _field(payload, "status", "change_request"),
                "change_request.status",
            )
        ),
        affected_requirement_ids=_strings(
            _field(payload, "affected_requirement_ids", "change_request"),
            "change_request.affected_requirement_ids",
        ),
        created_by=_expect_str(
            _field(payload, "created_by", "change_request"),
            "change_request.created_by",
        ),
        created_at=_datetime(
            _field(payload, "created_at", "change_request"),
            "change_request.created_at",
        ),
    )


def _impact_analysis_from_payload(value: object) -> ImpactAnalysis:
    payload = _expect_object(value, "impact_analysis")
    return ImpactAnalysis(
        change_request_id=_expect_str(
            _field(payload, "change_request_id", "impact_analysis"),
            "impact_analysis.change_request_id",
        ),
        architecture_impact=_expect_str(
            _field(payload, "architecture_impact", "impact_analysis"),
            "impact_analysis.architecture_impact",
        ),
        affected_components=_strings(
            _field(payload, "affected_components", "impact_analysis"),
            "impact_analysis.affected_components",
        ),
        affected_completed_tasks=_strings(
            _field(payload, "affected_completed_tasks", "impact_analysis"),
            "impact_analysis.affected_completed_tasks",
        ),
        affected_in_progress_tasks=_strings(
            _field(payload, "affected_in_progress_tasks", "impact_analysis"),
            "impact_analysis.affected_in_progress_tasks",
        ),
        affected_pending_tasks=_strings(
            _field(payload, "affected_pending_tasks", "impact_analysis"),
            "impact_analysis.affected_pending_tasks",
        ),
        tasks_to_add=_strings(
            _field(payload, "tasks_to_add", "impact_analysis"),
            "impact_analysis.tasks_to_add",
        ),
        tasks_to_reopen=_strings(
            _field(payload, "tasks_to_reopen", "impact_analysis"),
            "impact_analysis.tasks_to_reopen",
        ),
        tasks_to_cancel=_strings(
            _field(payload, "tasks_to_cancel", "impact_analysis"),
            "impact_analysis.tasks_to_cancel",
        ),
        recommendation=_expect_str(
            _field(payload, "recommendation", "impact_analysis"),
            "impact_analysis.recommendation",
        ),
        summary=_expect_str(
            _field(payload, "summary", "impact_analysis"),
            "impact_analysis.summary",
        ),
        affected_requirement_ids=_strings(
            _field(payload, "affected_requirement_ids", "impact_analysis"),
            "impact_analysis.affected_requirement_ids",
        ),
        affected_task_ids=_strings(
            _field(payload, "affected_task_ids", "impact_analysis"),
            "impact_analysis.affected_task_ids",
        ),
        requirements_to_add=_strings(
            _field(payload, "requirements_to_add", "impact_analysis"),
            "impact_analysis.requirements_to_add",
        ),
        requirements_to_update=_strings(
            _field(payload, "requirements_to_update", "impact_analysis"),
            "impact_analysis.requirements_to_update",
        ),
        milestone_ids=_strings(
            _field(payload, "milestone_ids", "impact_analysis"),
            "impact_analysis.milestone_ids",
        ),
        dependency_changes=_strings(
            _field(payload, "dependency_changes", "impact_analysis"),
            "impact_analysis.dependency_changes",
        ),
        risks=_strings(
            _field(payload, "risks", "impact_analysis"),
            "impact_analysis.risks",
        ),
        rationale=_expect_str(
            _field(payload, "rationale", "impact_analysis"),
            "impact_analysis.rationale",
        ),
    )


def _decision_from_payload(value: object) -> Decision:
    payload = _expect_object(value, "decision")
    return Decision(
        id=_expect_str(_field(payload, "id", "decision"), "decision.id"),
        task_id=_expect_optional_str(
            _field(payload, "task_id", "decision"), "decision.task_id"
        ),
        type=SupervisorDecisionType(
            _expect_str(_field(payload, "type", "decision"), "decision.type")
        ),
        rationale=_expect_str(
            _field(payload, "rationale", "decision"), "decision.rationale"
        ),
        created_at=_datetime(
            _field(payload, "created_at", "decision"), "decision.created_at"
        ),
    )


def _execution_report_from_payload(value: object) -> ExecutionReport:
    payload = _expect_object(value, "execution_report")
    return ExecutionReport(
        id=_expect_str(_field(payload, "id", "execution_report"), "execution_report.id"),
        task_id=_expect_str(
            _field(payload, "task_id", "execution_report"),
            "execution_report.task_id",
        ),
        attempt=_expect_int(
            _field(payload, "attempt", "execution_report"),
            "execution_report.attempt",
        ),
        status=_expect_str(
            _field(payload, "status", "execution_report"),
            "execution_report.status",
        ),
        files_changed=_strings(
            _field(payload, "files_changed", "execution_report"),
            "execution_report.files_changed",
        ),
        tests=_strings(
            _field(payload, "tests", "execution_report"), "execution_report.tests"
        ),
        static_checks=_strings(
            _field(payload, "static_checks", "execution_report"),
            "execution_report.static_checks",
        ),
        git_state=_expect_str(
            _field(payload, "git_state", "execution_report"),
            "execution_report.git_state",
        ),
        issues=_strings(
            _field(payload, "issues", "execution_report"),
            "execution_report.issues",
        ),
        human_action_required=_expect_bool(
            _field(payload, "human_action_required", "execution_report"),
            "execution_report.human_action_required",
        ),
        summary=_expect_str(
            _field(payload, "summary", "execution_report"),
            "execution_report.summary",
        ),
        created_at=_datetime(
            _field(payload, "created_at", "execution_report"),
            "execution_report.created_at",
        ),
    )


def _quality_status_from_payload(value: object) -> QualityStatus:
    payload = _expect_object(value, "quality_status")
    return QualityStatus(
        tests=_expect_str(
            _field(payload, "tests", "quality_status"), "quality_status.tests"
        ),
        lint=_expect_str(
            _field(payload, "lint", "quality_status"), "quality_status.lint"
        ),
        type_check=_expect_str(
            _field(payload, "type_check", "quality_status"),
            "quality_status.type_check",
        ),
        build=_expect_str(
            _field(payload, "build", "quality_status"), "quality_status.build"
        ),
        repository_clean=_expect_bool(
            _field(payload, "repository_clean", "quality_status"),
            "quality_status.repository_clean",
        ),
    )


def _event_from_payload(value: object) -> ProjectEvent:
    payload = _expect_object(value, "event")
    metadata_value = _expect_object(
        _field(payload, "metadata", "event"), "event.metadata"
    )
    metadata = {
        key: _expect_str(item, f"event.metadata.{key}")
        for key, item in metadata_value.items()
    }
    return ProjectEvent(
        id=_expect_str(_field(payload, "id", "event"), "event.id"),
        project_id=_expect_str(
            _field(payload, "project_id", "event"), "event.project_id"
        ),
        event_type=_expect_str(
            _field(payload, "event_type", "event"), "event.event_type"
        ),
        entity_id=_expect_optional_str(
            _field(payload, "entity_id", "event"), "event.entity_id"
        ),
        timestamp=_datetime(
            _field(payload, "timestamp", "event"), "event.timestamp"
        ),
        metadata=metadata,
    )


def _human_action_from_payload(value: object) -> HumanAction:
    payload = _expect_object(value, "human_action")
    resolved_value = _field(payload, "resolved_at", "human_action")
    return HumanAction(
        id=_expect_str(_field(payload, "id", "human_action"), "human_action.id"),
        project_id=_expect_str(
            _field(payload, "project_id", "human_action"),
            "human_action.project_id",
        ),
        task_id=_expect_optional_str(
            _field(payload, "task_id", "human_action"), "human_action.task_id"
        ),
        category=HumanActionCategory(
            _expect_str(
                _field(payload, "category", "human_action"),
                "human_action.category",
            )
        ),
        summary=_expect_str(
            _field(payload, "summary", "human_action"), "human_action.summary"
        ),
        requested_action=_expect_str(
            _field(payload, "requested_action", "human_action"),
            "human_action.requested_action",
        ),
        risk=_expect_str(
            _field(payload, "risk", "human_action"), "human_action.risk"
        ),
        status=HumanActionStatus(
            _expect_str(
                _field(payload, "status", "human_action"), "human_action.status"
            )
        ),
        created_at=_datetime(
            _field(payload, "created_at", "human_action"),
            "human_action.created_at",
        ),
        resolved_at=(
            None
            if resolved_value is None
            else _datetime(resolved_value, "human_action.resolved_at")
        ),
        worker_input=(
            None
            if _field(payload, "worker_input", "human_action") is None
            else _worker_input_from_payload(
                _field(payload, "worker_input", "human_action")
            )
        ),
    )


def _worker_input_from_payload(value: object) -> WorkerInputDetails:
    payload = _expect_object(value, "worker_input")
    return WorkerInputDetails(
        request_method=_expect_str(
            _field(payload, "request_method", "worker_input"),
            "worker_input.request_method",
        ),
        request_id=_expect_optional_str(
            _field(payload, "request_id", "worker_input"),
            "worker_input.request_id",
        ),
        question=_expect_str(
            _field(payload, "question", "worker_input"), "worker_input.question"
        ),
        choices=_strings(
            _field(payload, "choices", "worker_input"), "worker_input.choices"
        ),
        worker_attempt=_expect_int(
            _field(payload, "worker_attempt", "worker_input"),
            "worker_input.worker_attempt",
        ),
        baseline_head=_expect_optional_str(
            _field(payload, "baseline_head", "worker_input"),
            "worker_input.baseline_head",
        ),
        partial_paths=_strings(
            _field(payload, "partial_paths", "worker_input"),
            "worker_input.partial_paths",
        ),
        answer=_expect_optional_str(
            _field(payload, "answer", "worker_input"), "worker_input.answer"
        ),
    )


def _human_resolution_from_payload(value: object) -> HumanResolution:
    payload = _expect_object(value, "human_resolution")
    return HumanResolution(
        id=_expect_str(
            _field(payload, "id", "human_resolution"), "human_resolution.id"
        ),
        action_id=_expect_str(
            _field(payload, "action_id", "human_resolution"),
            "human_resolution.action_id",
        ),
        project_id=_expect_str(
            _field(payload, "project_id", "human_resolution"),
            "human_resolution.project_id",
        ),
        strategy=HumanResolutionStrategy(
            _expect_str(
                _field(payload, "strategy", "human_resolution"),
                "human_resolution.strategy",
            )
        ),
        summary=_expect_str(
            _field(payload, "summary", "human_resolution"),
            "human_resolution.summary",
        ),
        created_at=_datetime(
            _field(payload, "created_at", "human_resolution"),
            "human_resolution.created_at",
        ),
    )


def _execution_lease_from_payload(value: object) -> ExecutionLease:
    payload = _expect_object(value, "execution_lease")
    attempt_value = _field(payload, "attempt", "execution_lease")
    return ExecutionLease(
        id=_expect_str(
            _field(payload, "id", "execution_lease"), "execution_lease.id"
        ),
        project_id=_expect_str(
            _field(payload, "project_id", "execution_lease"),
            "execution_lease.project_id",
        ),
        owner_id=_expect_str(
            _field(payload, "owner_id", "execution_lease"),
            "execution_lease.owner_id",
        ),
        pid=_expect_int(
            _field(payload, "pid", "execution_lease"), "execution_lease.pid"
        ),
        acquired_at=_datetime(
            _field(payload, "acquired_at", "execution_lease"),
            "execution_lease.acquired_at",
        ),
        heartbeat_at=_datetime(
            _field(payload, "heartbeat_at", "execution_lease"),
            "execution_lease.heartbeat_at",
        ),
        status=ExecutionLeaseStatus(
            _expect_str(
                _field(payload, "status", "execution_lease"),
                "execution_lease.status",
            )
        ),
        current_task_id=_expect_optional_str(
            _field(payload, "current_task_id", "execution_lease"),
            "execution_lease.current_task_id",
        ),
        codex_thread_id=_expect_optional_str(
            _field(payload, "codex_thread_id", "execution_lease"),
            "execution_lease.codex_thread_id",
        ),
        attempt=(
            None
            if attempt_value is None
            else _expect_int(attempt_value, "execution_lease.attempt")
        ),
    )


def _git_baseline_from_payload(value: object) -> GitBaseline:
    payload = _expect_object(value, "git_baseline")
    return GitBaseline(
        task_id=_expect_str(
            _field(payload, "task_id", "git_baseline"), "git_baseline.task_id"
        ),
        repository_root=_expect_str(
            _field(payload, "repository_root", "git_baseline"),
            "git_baseline.repository_root",
        ),
        baseline_head=_expect_str(
            _field(payload, "baseline_head", "git_baseline"),
            "git_baseline.baseline_head",
        ),
        status_entries=_strings(
            _field(payload, "status_entries", "git_baseline"),
            "git_baseline.status_entries",
        ),
    )


def _git_change_set_from_payload(value: object) -> GitChangeSet:
    payload = _expect_object(value, "git_change_set")
    return GitChangeSet(
        task_id=_expect_str(
            _field(payload, "task_id", "git_change_set"),
            "git_change_set.task_id",
        ),
        repository_root=_expect_str(
            _field(payload, "repository_root", "git_change_set"),
            "git_change_set.repository_root",
        ),
        baseline_head=_expect_str(
            _field(payload, "baseline_head", "git_change_set"),
            "git_change_set.baseline_head",
        ),
        changed_paths=_strings(
            _field(payload, "changed_paths", "git_change_set"),
            "git_change_set.changed_paths",
        ),
        untracked_paths=_strings(
            _field(payload, "untracked_paths", "git_change_set"),
            "git_change_set.untracked_paths",
        ),
        staged_paths=_strings(
            _field(payload, "staged_paths", "git_change_set"),
            "git_change_set.staged_paths",
        ),
    )


def _git_commit_result_from_payload(value: object) -> GitCommitResult:
    payload = _expect_object(value, "git_commit_result")
    return GitCommitResult(
        task_id=_expect_str(
            _field(payload, "task_id", "git_commit_result"),
            "git_commit_result.task_id",
        ),
        repository_root=_expect_str(
            _field(payload, "repository_root", "git_commit_result"),
            "git_commit_result.repository_root",
        ),
        baseline_head=_expect_str(
            _field(payload, "baseline_head", "git_commit_result"),
            "git_commit_result.baseline_head",
        ),
        commit_sha=_expect_str(
            _field(payload, "commit_sha", "git_commit_result"),
            "git_commit_result.commit_sha",
        ),
        commit_message=_expect_str(
            _field(payload, "commit_message", "git_commit_result"),
            "git_commit_result.commit_message",
        ),
        changed_paths=_strings(
            _field(payload, "changed_paths", "git_commit_result"),
            "git_commit_result.changed_paths",
        ),
        staged_paths=_strings(
            _field(payload, "staged_paths", "git_commit_result"),
            "git_commit_result.staged_paths",
        ),
        committed_at=_datetime(
            _field(payload, "committed_at", "git_commit_result"),
            "git_commit_result.committed_at",
        ),
    )


def _verification_command_from_payload(
    value: object,
) -> ProjectVerificationCommand:
    payload = _expect_object(value, "project_verification_command")
    return ProjectVerificationCommand(
        name=_expect_str(
            _field(payload, "name", "project_verification_command"),
            "project_verification_command.name",
        ),
        category=ProjectVerificationCategory(
            _expect_str(
                _field(payload, "category", "project_verification_command"),
                "project_verification_command.category",
            )
        ),
        command=_strings(
            _field(payload, "command", "project_verification_command"),
            "project_verification_command.command",
        ),
        required=_expect_bool(
            _field(payload, "required", "project_verification_command"),
            "project_verification_command.required",
        ),
        timeout_seconds=_expect_number(
            _field(payload, "timeout_seconds", "project_verification_command"),
            "project_verification_command.timeout_seconds",
        ),
        network_allowed=_expect_bool(
            _field(payload, "network_allowed", "project_verification_command"),
            "project_verification_command.network_allowed",
        ),
    )


def _verification_spec_from_payload(value: object) -> ProjectVerificationSpec:
    payload = _expect_object(value, "project_verification_spec")
    return ProjectVerificationSpec(
        project_id=_expect_str(
            _field(payload, "project_id", "project_verification_spec"),
            "project_verification_spec.project_id",
        ),
        commands=_tuple_of(
            _field(payload, "commands", "project_verification_spec"),
            "project_verification_spec.commands",
            _verification_command_from_payload,
        ),
    )


def _verification_check_from_payload(value: object) -> ProjectVerificationCheck:
    payload = _expect_object(value, "project_verification_check")
    exit_code = _field(payload, "exit_code", "project_verification_check")
    return ProjectVerificationCheck(
        name=_expect_str(
            _field(payload, "name", "project_verification_check"),
            "project_verification_check.name",
        ),
        category=ProjectVerificationCategory(
            _expect_str(
                _field(payload, "category", "project_verification_check"),
                "project_verification_check.category",
            )
        ),
        command=_strings(
            _field(payload, "command", "project_verification_check"),
            "project_verification_check.command",
        ),
        status=ProjectVerificationStatus(
            _expect_str(
                _field(payload, "status", "project_verification_check"),
                "project_verification_check.status",
            )
        ),
        exit_code=(
            None
            if exit_code is None
            else _expect_int(exit_code, "project_verification_check.exit_code")
        ),
        safe_summary=_expect_str(
            _field(payload, "safe_summary", "project_verification_check"),
            "project_verification_check.safe_summary",
        ),
        required=_expect_bool(
            _field(payload, "required", "project_verification_check"),
            "project_verification_check.required",
        ),
    )


def _verification_result_from_payload(value: object) -> ProjectVerificationResult:
    payload = _expect_object(value, "project_verification_result")
    decision = _field(payload, "final_review_decision", "project_verification_result")
    return ProjectVerificationResult(
        id=_expect_str(
            _field(payload, "id", "project_verification_result"),
            "project_verification_result.id",
        ),
        project_id=_expect_str(
            _field(payload, "project_id", "project_verification_result"),
            "project_verification_result.project_id",
        ),
        plan_id=_expect_str(
            _field(payload, "plan_id", "project_verification_result"),
            "project_verification_result.plan_id",
        ),
        expected_head=_expect_str(
            _field(payload, "expected_head", "project_verification_result"),
            "project_verification_result.expected_head",
        ),
        verified_head=_expect_str(
            _field(payload, "verified_head", "project_verification_result"),
            "project_verification_result.verified_head",
        ),
        checks=_tuple_of(
            _field(payload, "checks", "project_verification_result"),
            "project_verification_result.checks",
            _verification_check_from_payload,
        ),
        started_at=_datetime(
            _field(payload, "started_at", "project_verification_result"),
            "project_verification_result.started_at",
        ),
        completed_at=_datetime(
            _field(payload, "completed_at", "project_verification_result"),
            "project_verification_result.completed_at",
        ),
        final_review_decision=(
            None
            if decision is None
            else FinalReviewDecision(
                _expect_str(decision, "project_verification_result.final_review_decision")
            )
        ),
        final_review_summary=_expect_optional_str(
            _field(payload, "final_review_summary", "project_verification_result"),
            "project_verification_result.final_review_summary",
        ),
    )


def deserialize_project_state(payload: dict[str, object]) -> ProjectState:
    """Restore a complete snapshot, rejecting unknown or corrupt payloads."""

    root = _expect_object(payload, "project state")
    if "schema_version" not in root:
        raise UnsupportedStateSchema("schema_version is required")
    schema_version = root["schema_version"]
    if type(schema_version) is not int:
        raise UnsupportedStateSchema("schema_version must be an integer")
    if schema_version not in {1, 2, 3, 4, 5, 6, 7, 8, CURRENT_SCHEMA_VERSION}:
        raise UnsupportedStateSchema(
            f"unsupported schema_version: {schema_version!r}"
        )
    if schema_version == 1:
        root = _migrate_v1_to_v2(root)
        schema_version = 2
    if schema_version == 2:
        root = _migrate_v2_to_v3(root)
        schema_version = 3
    if schema_version == 3:
        root = _migrate_v3_to_v4(root)
        schema_version = 4
    if schema_version == 4:
        root = _migrate_v4_to_v5(root)
        schema_version = 5
    if schema_version == 5:
        root = _migrate_v5_to_v6(root)
        schema_version = 6
    if schema_version == 6:
        root = _migrate_v6_to_v7(root)
        schema_version = 7
    if schema_version == 7:
        root = _migrate_v7_to_v8(root)
        schema_version = 8
    if schema_version == 8:
        root = _migrate_v8_to_v9(root)

    try:
        quality_value = _field(root, "quality_status", "project state")
        quality_status = (
            None
            if quality_value is None
            else _quality_status_from_payload(quality_value)
        )
        return ProjectState(
            project=_project_from_payload(
                _field(root, "project", "project state")
            ),
            requirements=_tuple_of(
                _field(root, "requirements", "project state"),
                "requirements",
                _requirement_from_payload,
            ),
            plans=_tuple_of(
                _field(root, "plans", "project state"),
                "plans",
                _plan_from_payload,
            ),
            milestones=_tuple_of(
                _field(root, "milestones", "project state"),
                "milestones",
                _milestone_from_payload,
            ),
            tasks=_tuple_of(
                _field(root, "tasks", "project state"),
                "tasks",
                _task_from_payload,
            ),
            change_requests=_tuple_of(
                _field(root, "change_requests", "project state"),
                "change_requests",
                _change_request_from_payload,
            ),
            impact_analyses=_tuple_of(
                _field(root, "impact_analyses", "project state"),
                "impact_analyses",
                _impact_analysis_from_payload,
            ),
            decisions=_tuple_of(
                _field(root, "decisions", "project state"),
                "decisions",
                _decision_from_payload,
            ),
            execution_reports=_tuple_of(
                _field(root, "execution_reports", "project state"),
                "execution_reports",
                _execution_report_from_payload,
            ),
            quality_status=quality_status,
            events=_tuple_of(
                _field(root, "events", "project state"),
                "events",
                _event_from_payload,
            ),
            human_actions=_tuple_of(
                _field(root, "human_actions", "project state"),
                "human_actions",
                _human_action_from_payload,
            ),
            human_resolutions=_tuple_of(
                _field(root, "human_resolutions", "project state"),
                "human_resolutions",
                _human_resolution_from_payload,
            ),
            execution_leases=_tuple_of(
                _field(root, "execution_leases", "project state"),
                "execution_leases",
                _execution_lease_from_payload,
            ),
            git_baselines=_tuple_of(
                _field(root, "git_baselines", "project state"),
                "git_baselines",
                _git_baseline_from_payload,
            ),
            git_change_sets=_tuple_of(
                _field(root, "git_change_sets", "project state"),
                "git_change_sets",
                _git_change_set_from_payload,
            ),
            git_commit_results=_tuple_of(
                _field(root, "git_commit_results", "project state"),
                "git_commit_results",
                _git_commit_result_from_payload,
            ),
            project_verification_spec=(
                None
                if _field(root, "project_verification_spec", "project state") is None
                else _verification_spec_from_payload(
                    _field(root, "project_verification_spec", "project state")
                )
            ),
            project_verification_results=_tuple_of(
                _field(root, "project_verification_results", "project state"),
                "project_verification_results",
                _verification_result_from_payload,
            ),
        )
    except InvalidProjectState:
        raise
    except (TypeError, ValueError) as error:
        raise InvalidProjectState("project state contains invalid data") from error


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "InvalidProjectState",
    "UnsupportedStateSchema",
    "deserialize_project_state",
    "serialize_project_state",
]
