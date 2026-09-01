"""Strict local validation for untrusted Supervisor structured output."""

from collections.abc import Callable
from typing import TypeVar, cast

from code_mule.domain.enums import SupervisorDecisionType

from .contracts import (
    ImpactAnalysisResult,
    MilestoneProposal,
    PlanProposal,
    ProgressReport,
    RequirementProposal,
    RequirementUpdateProposal,
    ReviewResult,
    TaskDependencyChange,
    TaskProposal,
)


class InvalidSupervisorResponse(ValueError):
    """Raised when model output violates a Supervisor response contract."""


_R = TypeVar("_R")


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InvalidSupervisorResponse(f"{context} must be an object")
    return cast(dict[str, object], value)


def _exact_fields(
    payload: dict[str, object],
    required: set[str],
    context: str,
) -> None:
    actual = set(payload)
    missing = required - actual
    extra = actual - required
    if missing:
        raise InvalidSupervisorResponse(
            f"{context} is missing fields: {', '.join(sorted(missing))}"
        )
    if extra:
        raise InvalidSupervisorResponse(
            f"{context} contains extra fields: {', '.join(sorted(extra))}"
        )


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise InvalidSupervisorResponse(f"{context} must be a string")
    return value


def _optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _string(value, context)


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidSupervisorResponse(f"{context} must be an array")
    return cast(list[object], value)


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{context}[{index}]")
        for index, item in enumerate(_array(value, context))
    )


def _task_proposal(value: object, context: str) -> TaskProposal:
    payload = _object(value, context)
    _exact_fields(
        payload,
        {
            "id",
            "title",
            "description",
            "dependencies",
            "acceptance_criteria",
            "requirement_ids",
        },
        context,
    )
    return TaskProposal(
        id=_string(payload["id"], f"{context}.id"),
        title=_string(payload["title"], f"{context}.title"),
        description=_string(payload["description"], f"{context}.description"),
        dependencies=_string_tuple(
            payload["dependencies"], f"{context}.dependencies"
        ),
        acceptance_criteria=_string_tuple(
            payload["acceptance_criteria"], f"{context}.acceptance_criteria"
        ),
        requirement_ids=_string_tuple(
            payload["requirement_ids"], f"{context}.requirement_ids"
        ),
    )


def _requirement_proposal(value: object, context: str) -> RequirementProposal:
    payload = _object(value, context)
    _exact_fields(
        payload,
        {"id", "title", "description", "priority", "acceptance_criteria"},
        context,
    )
    return RequirementProposal(
        id=_string(payload["id"], f"{context}.id"),
        title=_string(payload["title"], f"{context}.title"),
        description=_string(payload["description"], f"{context}.description"),
        priority=_string(payload["priority"], f"{context}.priority"),
        acceptance_criteria=_string_tuple(
            payload["acceptance_criteria"], f"{context}.acceptance_criteria"
        ),
    )


def _milestone_proposal(value: object, context: str) -> MilestoneProposal:
    payload = _object(value, context)
    _exact_fields(payload, {"id", "title", "task_ids"}, context)
    return MilestoneProposal(
        id=_string(payload["id"], f"{context}.id"),
        title=_string(payload["title"], f"{context}.title"),
        task_ids=_string_tuple(payload["task_ids"], f"{context}.task_ids"),
    )


def _requirement_update_proposal(
    value: object, context: str
) -> RequirementUpdateProposal:
    payload = _object(value, context)
    _exact_fields(payload, {"supersedes_id", "requirement"}, context)
    return RequirementUpdateProposal(
        supersedes_id=_string(
            payload["supersedes_id"], f"{context}.supersedes_id"
        ),
        requirement=_requirement_proposal(
            payload["requirement"], f"{context}.requirement"
        ),
    )


def _dependency_change(value: object, context: str) -> TaskDependencyChange:
    payload = _object(value, context)
    _exact_fields(payload, {"task_id", "dependencies"}, context)
    return TaskDependencyChange(
        task_id=_string(payload["task_id"], f"{context}.task_id"),
        dependencies=_string_tuple(
            payload["dependencies"], f"{context}.dependencies"
        ),
    )


def _parse(parse_operation: Callable[[], _R]) -> _R:
    try:
        return parse_operation()
    except InvalidSupervisorResponse:
        raise
    except (TypeError, ValueError) as error:
        raise InvalidSupervisorResponse("response violates its contract") from error


def parse_plan_response(payload: dict[str, object]) -> PlanProposal:
    def parse() -> PlanProposal:
        root = _object(payload, "plan response")
        _exact_fields(
            root,
            {
                "summary",
                "requirements",
                "requirements_considered",
                "milestones",
                "tasks",
                "risks",
                "rationale",
            },
            "plan response",
        )
        milestones = tuple(
            _milestone_proposal(item, f"milestones[{index}]")
            for index, item in enumerate(_array(root["milestones"], "milestones"))
        )
        tasks = tuple(
            _task_proposal(item, f"tasks[{index}]")
            for index, item in enumerate(_array(root["tasks"], "tasks"))
        )
        return PlanProposal(
            summary=_string(root["summary"], "summary"),
            requirements=tuple(
                _requirement_proposal(item, f"requirements[{index}]")
                for index, item in enumerate(
                    _array(root["requirements"], "requirements")
                )
            ),
            requirements_considered=_string_tuple(
                root["requirements_considered"], "requirements_considered"
            ),
            milestones=milestones,
            tasks=tasks,
            risks=_string_tuple(root["risks"], "risks"),
            rationale=_string(root["rationale"], "rationale"),
        )

    return _parse(parse)


def parse_review_response(payload: dict[str, object]) -> ReviewResult:
    def parse() -> ReviewResult:
        root = _object(payload, "review response")
        _exact_fields(
            root,
            {"decision", "rationale", "next_task_prompt", "issues"},
            "review response",
        )
        return ReviewResult(
            decision=SupervisorDecisionType(
                _string(root["decision"], "decision")
            ),
            rationale=_string(root["rationale"], "rationale"),
            next_task_prompt=_optional_string(
                root["next_task_prompt"], "next_task_prompt"
            ),
            issues=_string_tuple(root["issues"], "issues"),
        )

    return _parse(parse)


def parse_impact_analysis_response(
    payload: dict[str, object],
) -> ImpactAnalysisResult:
    def parse() -> ImpactAnalysisResult:
        root = _object(payload, "impact analysis response")
        _exact_fields(
            root,
            {
                "change_request_id",
                "summary",
                "architecture_impact",
                "affected_components",
                "affected_requirement_ids",
                "affected_task_ids",
                "affected_completed_tasks",
                "affected_in_progress_tasks",
                "affected_pending_tasks",
                "requirements_to_add",
                "requirements_to_update",
                "tasks_to_add",
                "tasks_to_reopen",
                "tasks_to_cancel",
                "milestone_ids_reused",
                "milestones",
                "dependency_changes",
                "risks",
                "recommendation",
                "rationale",
            },
            "impact analysis response",
        )
        tasks_to_add = tuple(
            _task_proposal(item, f"tasks_to_add[{index}]")
            for index, item in enumerate(
                _array(root["tasks_to_add"], "tasks_to_add")
            )
        )
        return ImpactAnalysisResult(
            change_request_id=_string(
                root["change_request_id"], "change_request_id"
            ),
            summary=_string(root["summary"], "summary"),
            architecture_impact=_string(
                root["architecture_impact"], "architecture_impact"
            ),
            affected_components=_string_tuple(
                root["affected_components"], "affected_components"
            ),
            affected_requirement_ids=_string_tuple(
                root["affected_requirement_ids"], "affected_requirement_ids"
            ),
            affected_task_ids=_string_tuple(
                root["affected_task_ids"], "affected_task_ids"
            ),
            affected_completed_tasks=_string_tuple(
                root["affected_completed_tasks"], "affected_completed_tasks"
            ),
            affected_in_progress_tasks=_string_tuple(
                root["affected_in_progress_tasks"],
                "affected_in_progress_tasks",
            ),
            affected_pending_tasks=_string_tuple(
                root["affected_pending_tasks"], "affected_pending_tasks"
            ),
            requirements_to_add=tuple(
                _requirement_proposal(item, f"requirements_to_add[{index}]")
                for index, item in enumerate(
                    _array(root["requirements_to_add"], "requirements_to_add")
                )
            ),
            requirements_to_update=tuple(
                _requirement_update_proposal(
                    item, f"requirements_to_update[{index}]"
                )
                for index, item in enumerate(
                    _array(
                        root["requirements_to_update"],
                        "requirements_to_update",
                    )
                )
            ),
            tasks_to_add=tasks_to_add,
            tasks_to_reopen=_string_tuple(
                root["tasks_to_reopen"], "tasks_to_reopen"
            ),
            tasks_to_cancel=_string_tuple(
                root["tasks_to_cancel"], "tasks_to_cancel"
            ),
            milestone_ids_reused=_string_tuple(
                root["milestone_ids_reused"], "milestone_ids_reused"
            ),
            milestones=tuple(
                _milestone_proposal(item, f"milestones[{index}]")
                for index, item in enumerate(
                    _array(root["milestones"], "milestones")
                )
            ),
            dependency_changes=tuple(
                _dependency_change(item, f"dependency_changes[{index}]")
                for index, item in enumerate(
                    _array(root["dependency_changes"], "dependency_changes")
                )
            ),
            risks=_string_tuple(root["risks"], "risks"),
            recommendation=_string(root["recommendation"], "recommendation"),
            rationale=_string(root["rationale"], "rationale"),
        )

    return _parse(parse)


def parse_progress_report_response(
    payload: dict[str, object],
) -> ProgressReport:
    def parse() -> ProgressReport:
        root = _object(payload, "progress report response")
        _exact_fields(
            root,
            {
                "summary",
                "current_status",
                "current_work",
                "completed",
                "remaining",
                "blockers",
                "risks",
                "quality_summary",
            },
            "progress report response",
        )
        return ProgressReport(
            summary=_string(root["summary"], "summary"),
            current_status=_string(root["current_status"], "current_status"),
            current_work=_optional_string(root["current_work"], "current_work"),
            completed=_string_tuple(root["completed"], "completed"),
            remaining=_string_tuple(root["remaining"], "remaining"),
            blockers=_string_tuple(root["blockers"], "blockers"),
            risks=_string_tuple(root["risks"], "risks"),
            quality_summary=_optional_string(
                root["quality_summary"], "quality_summary"
            ),
        )

    return _parse(parse)


__all__ = [
    "InvalidSupervisorResponse",
    "parse_impact_analysis_response",
    "parse_plan_response",
    "parse_progress_report_response",
    "parse_review_response",
]
