"""Fresh JSON Schema contracts for Supervisor structured output."""


def _strict_object(
    properties: dict[str, object],
    required: list[str],
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _string_array() -> dict[str, object]:
    return {"type": "array", "items": {"type": "string"}}


def _non_empty_string_array() -> dict[str, object]:
    return {"type": "array", "items": {"type": "string"}, "minItems": 1}


def _task_proposal_schema() -> dict[str, object]:
    return _strict_object(
        {
            "id": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "description": {"type": "string", "minLength": 1},
            "dependencies": _string_array(),
            "acceptance_criteria": _non_empty_string_array(),
            "requirement_ids": _non_empty_string_array(),
        },
        [
            "id",
            "title",
            "description",
            "dependencies",
            "acceptance_criteria",
            "requirement_ids",
        ],
    )


def _requirement_proposal_schema() -> dict[str, object]:
    return _strict_object(
        {
            "id": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "description": {"type": "string", "minLength": 1},
            "priority": {"type": "string", "minLength": 1},
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
        ["id", "title", "description", "priority", "acceptance_criteria"],
    )


def plan_response_schema() -> dict[str, object]:
    milestone_schema = _strict_object(
        {
            "id": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "task_ids": _string_array(),
        },
        ["id", "title", "task_ids"],
    )
    return _strict_object(
        {
            "summary": {"type": "string", "minLength": 1},
            "requirements": {
                "type": "array",
                "items": _requirement_proposal_schema(),
            },
            "requirements_considered": _string_array(),
            "milestones": {"type": "array", "items": milestone_schema},
            "tasks": {"type": "array", "items": _task_proposal_schema()},
            "risks": _string_array(),
            "rationale": {"type": "string", "minLength": 1},
        },
        [
            "summary",
            "requirements",
            "requirements_considered",
            "milestones",
            "tasks",
            "risks",
            "rationale",
        ],
    )


def review_response_schema() -> dict[str, object]:
    return _strict_object(
        {
            "decision": {
                "type": "string",
                "enum": ["continue", "rework", "human_required", "done"],
            },
            "rationale": {"type": "string"},
            "next_task_prompt": {"type": ["string", "null"]},
            "issues": _string_array(),
        },
        ["decision", "rationale", "next_task_prompt", "issues"],
    )


def impact_analysis_response_schema() -> dict[str, object]:
    return _strict_object(
        {
            "architecture_impact": {"type": "string"},
            "affected_components": _string_array(),
            "affected_completed_tasks": _string_array(),
            "affected_in_progress_tasks": _string_array(),
            "affected_pending_tasks": _string_array(),
            "tasks_to_add": {
                "type": "array",
                "items": _task_proposal_schema(),
            },
            "tasks_to_reopen": _string_array(),
            "tasks_to_cancel": _string_array(),
            "recommendation": {"type": "string", "minLength": 1},
            "rationale": {"type": "string", "minLength": 1},
        },
        [
            "architecture_impact",
            "affected_components",
            "affected_completed_tasks",
            "affected_in_progress_tasks",
            "affected_pending_tasks",
            "tasks_to_add",
            "tasks_to_reopen",
            "tasks_to_cancel",
            "recommendation",
            "rationale",
        ],
    )


def progress_report_response_schema() -> dict[str, object]:
    return _strict_object(
        {
            "summary": {"type": "string"},
            "current_status": {"type": "string"},
            "current_work": {"type": ["string", "null"]},
            "completed": _string_array(),
            "remaining": _string_array(),
            "blockers": _string_array(),
            "risks": _string_array(),
            "quality_summary": {"type": ["string", "null"]},
        },
        [
            "summary",
            "current_status",
            "current_work",
            "completed",
            "remaining",
            "blockers",
            "risks",
            "quality_summary",
        ],
    )


__all__ = [
    "impact_analysis_response_schema",
    "plan_response_schema",
    "progress_report_response_schema",
    "review_response_schema",
]
