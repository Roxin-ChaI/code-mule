"""Explicit JSON-compatible serialization for project-state snapshots."""

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

from code_mule.domain.enums import (
    ChangeRequestStatus,
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
    Milestone,
    Plan,
    Project,
    ProjectEvent,
    QualityStatus,
    Requirement,
    Task,
)

from .models import ProjectState


CURRENT_SCHEMA_VERSION = 3


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
    migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    migrated["requirements"] = migrated_requirements
    migrated["impact_analyses"] = migrated_impacts
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


def deserialize_project_state(payload: dict[str, object]) -> ProjectState:
    """Restore a complete snapshot, rejecting unknown or corrupt payloads."""

    root = _expect_object(payload, "project state")
    if "schema_version" not in root:
        raise UnsupportedStateSchema("schema_version is required")
    schema_version = root["schema_version"]
    if type(schema_version) is not int:
        raise UnsupportedStateSchema("schema_version must be an integer")
    if schema_version not in {1, 2, CURRENT_SCHEMA_VERSION}:
        raise UnsupportedStateSchema(
            f"unsupported schema_version: {schema_version!r}"
        )
    if schema_version == 1:
        root = _migrate_v1_to_v2(root)
        schema_version = 2
    if schema_version == 2:
        root = _migrate_v2_to_v3(root)

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
