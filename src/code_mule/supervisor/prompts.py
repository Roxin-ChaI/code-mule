"""Deterministic prompts derived only from ProjectState and the current request."""

import json

from code_mule.domain.enums import ChangeRequestStatus
from code_mule.domain.models import (
    ChangeRequest,
    Decision,
    ExecutionReport,
    Milestone,
    Plan,
    ProjectEvent,
    Requirement,
    Task,
)
from code_mule.state.models import ProjectState

from .contracts import (
    ImpactAnalysisRequest,
    PlanRequest,
    ProgressReportRequest,
    ReviewRequest,
    SupervisorOperation,
)


SUPERVISOR_SYSTEM_POLICY = """You are the Supervisor for Code Mule.
Responsibilities: reason about project state, make proposals, review execution evidence, and explain progress.
ProjectState is the source of truth. Distinguish recorded facts from proposals.
Do not claim repository actions were executed. Do not claim tests were executed unless evidence exists.
Do not modify project state, bypass a Human Gate, authorize irreversible external operations, invent completed work, or assume missing evidence is PASS.
When evidence is insufficient, fail conservatively and state what is unknown.
Progress reports must use only the supplied ProjectState; a task is not completed without recorded COMPLETED status, and verification or quality is not PASS without evidence.
The Supervisor cannot approve Human Gates. For git push, force push, tag, GitHub Release, destructive file deletion, destructive migration, deployment, secret/API key usage, paid external API use, or any irreversible external side effect, recommend HUMAN_REQUIRED. Enforcement belongs to the Orchestrator.
For PLAN, propose executable work rather than a vague roadmap: requirements need verifiable acceptance criteria; tasks must be independently reviewable, fit one Codex execution cycle, trace to at least one proposed or existing requirement, and use only declared task dependencies. Avoid over-design and do not add deployment or release work unless the Boss objective explicitly requires it. Human Gate operations must never be proposed as silently automatic actions.
Produce structured output matching the required schema exactly."""


def _text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _optional_text(value: str | None) -> str:
    return "null" if value is None else _text(value)


def _string_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _render_requirement(item: Requirement) -> str:
    return (
        f"- id={_text(item.id)} title={_text(item.title)} "
        f"status={item.status.value} priority={_text(item.priority)} "
        f"description={_text(item.description)} "
        f"acceptance_criteria={_string_list(item.acceptance_criteria)}"
    )


def _render_plan(item: Plan) -> str:
    return (
        f"- id={_text(item.id)} version={item.version} status={item.status.value} "
        f"requirement_ids={_string_list(item.requirement_ids)} "
        f"milestone_ids={_string_list(item.milestone_ids)}"
    )


def _render_milestone(item: Milestone) -> str:
    return (
        f"- id={_text(item.id)} title={_text(item.title)} "
        f"status={_text(item.status)} task_ids={_string_list(item.task_ids)}"
    )


def _render_task(item: Task) -> str:
    return (
        f"- id={_text(item.id)} title={_text(item.title)} status={item.status.value} "
        f"milestone_id={_text(item.milestone_id)} "
        f"description={_text(item.description)} "
        f"requirement_ids={_string_list(item.requirement_ids)} "
        f"dependencies={_string_list(item.dependencies)} "
        f"acceptance_criteria={_string_list(item.acceptance_criteria)} "
        f"execution_attempts={item.execution_attempts}"
    )


def _render_change_request(item: ChangeRequest) -> str:
    return (
        f"- id={_text(item.id)} status={item.status.value} "
        f"description={_text(item.description)} created_by={_text(item.created_by)} "
        f"affected_requirement_ids={_string_list(item.affected_requirement_ids)}"
    )


def _render_decision(item: Decision) -> str:
    return (
        f"- id={_text(item.id)} task_id={_optional_text(item.task_id)} "
        f"type={item.type.value} rationale={_text(item.rationale)} "
        f"created_at={item.created_at.isoformat()}"
    )


def _render_execution_report(item: ExecutionReport) -> str:
    return (
        f"- id={_text(item.id)} task_id={_text(item.task_id)} attempt={item.attempt} "
        f"status={_text(item.status)} tests={_string_list(item.tests)} "
        f"static_checks={_string_list(item.static_checks)} "
        f"issues={_string_list(item.issues)} "
        f"human_action_required={str(item.human_action_required).lower()} "
        f"summary={_text(item.summary)} created_at={item.created_at.isoformat()}"
    )


def _render_event(item: ProjectEvent) -> str:
    return (
        f"- id={_text(item.id)} event_type={_text(item.event_type)} "
        f"entity_id={_optional_text(item.entity_id)} "
        f"timestamp={item.timestamp.isoformat()} "
        f"metadata={json.dumps(item.metadata, ensure_ascii=False, sort_keys=True)}"
    )


def _append_section(lines: list[str], heading: str, entries: list[str]) -> None:
    lines.append(heading)
    lines.extend(entries if entries else ["- none"])


def _render_project_state(state: ProjectState) -> str:
    lines = [
        "Project State Snapshot",
        "Project:",
        f"- id={_text(state.project.id)}",
        f"- name={_text(state.project.name)}",
        f"- status={state.project.status.value}",
        f"- active_plan_id={_optional_text(state.project.active_plan_id)}",
        f"- current_task_id={_optional_text(state.project.current_task_id)}",
    ]

    _append_section(
        lines,
        "Requirements (complete, original order):",
        [_render_requirement(item) for item in state.requirements],
    )

    active_plan = next(
        (item for item in state.plans if item.id == state.project.active_plan_id),
        None,
    )
    _append_section(
        lines,
        "Active Plan:",
        [] if active_plan is None else [_render_plan(active_plan)],
    )

    active_milestones = ()
    if active_plan is not None:
        declared_milestone_ids = set(active_plan.milestone_ids)
        active_milestones = tuple(
            item
            for item in state.milestones
            if item.plan_id == active_plan.id and item.id in declared_milestone_ids
        )
    active_tasks = tuple(
        item
        for item in state.tasks
        if any(
            item.milestone_id == milestone.id and item.id in milestone.task_ids
            for milestone in active_milestones
        )
    )
    _append_section(
        lines,
        "Active Plan Milestones (original order):",
        [_render_milestone(item) for item in active_milestones],
    )
    _append_section(
        lines,
        "Active Plan Tasks (original order):",
        [_render_task(item) for item in active_tasks],
    )

    open_statuses = {
        ChangeRequestStatus.PENDING,
        ChangeRequestStatus.ANALYZING,
    }
    open_changes = tuple(
        item for item in state.change_requests if item.status in open_statuses
    )
    _append_section(
        lines,
        "Open Change Requests (original order):",
        [_render_change_request(item) for item in open_changes],
    )

    lines.append("Quality Status:")
    if state.quality_status is None:
        lines.append("- unknown; no quality evidence recorded")
    else:
        quality = state.quality_status
        lines.append(
            f"- tests={_text(quality.tests)} lint={_text(quality.lint)} "
            f"type_check={_text(quality.type_check)} build={_text(quality.build)} "
            f"repository_clean={str(quality.repository_clean).lower()}"
        )

    lines.append(
        "Historical entries may be truncated; retained entries preserve original order."
    )
    _append_section(
        lines,
        "Recent Decisions (last 10):",
        [_render_decision(item) for item in state.decisions[-10:]],
    )
    _append_section(
        lines,
        "Recent Execution Reports (last 10):",
        [_render_execution_report(item) for item in state.execution_reports[-10:]],
    )
    _append_section(
        lines,
        "Recent Events (last 20):",
        [_render_event(item) for item in state.events[-20:]],
    )
    return "\n".join(lines)


def _system_prompt(operation: SupervisorOperation) -> str:
    return f"{SUPERVISOR_SYSTEM_POLICY}\nCurrent operation: {operation.value}."


def build_plan_prompt(request: PlanRequest) -> tuple[str, str]:
    existing_requirement_ids = tuple(
        item.id for item in request.project_state.requirements
    )
    user_prompt = (
        f"{_render_project_state(request.project_state)}\n\n"
        "Current operation context:\n"
        "Operation: PLAN\n"
        f"Objective: {_text(request.objective)}\n"
        f"Existing Requirement IDs: {_string_list(existing_requirement_ids)}\n"
        "requirements_considered may contain only IDs from Existing Requirement "
        "IDs; never put explanations, notes, titles, or other natural language "
        "in that field. Put new requirements in requirements, not in "
        "requirements_considered. If there are no existing Requirements, return "
        'exactly "requirements_considered": []. Example — Existing requirements: '
        'none. Correct: "requirements_considered": []. Incorrect: '
        '"requirements_considered": ["No existing requirements exist"].\n'
        "Decompose the objective into the smallest deliverable requirements with "
        "verifiable acceptance criteria, milestones, and independently reviewable "
        "tasks sized for one Codex execution cycle. Every task must reference at "
        "least one proposed or existing requirement. Dependencies must reference "
        "declared task IDs. Keep scope appropriate for v0.1.0, avoid over-design, "
        "and do not add deployment or release work unless explicitly requested. "
        "Return a planning proposal only; do not create or apply a domain Plan."
    )
    return _system_prompt(SupervisorOperation.PLAN), user_prompt


def build_review_prompt(request: ReviewRequest) -> tuple[str, str]:
    user_prompt = (
        f"{_render_project_state(request.project_state)}\n\n"
        "Current operation context:\n"
        "Operation: REVIEW\n"
        f"Task under review:\n{_render_task(request.task)}\n"
        "Execution report under review:\n"
        f"{_render_execution_report(request.execution_report)}\n"
        "Return a review decision based only on recorded evidence."
    )
    return _system_prompt(SupervisorOperation.REVIEW), user_prompt


def build_impact_analysis_prompt(
    request: ImpactAnalysisRequest,
) -> tuple[str, str]:
    state = request.project_state
    active_plan = next(
        (item for item in state.plans if item.id == state.project.active_plan_id),
        None,
    )
    active_milestone_ids = () if active_plan is None else active_plan.milestone_ids
    active_task_ids = tuple(
        task_id
        for milestone_id in active_milestone_ids
        for milestone in state.milestones
        if milestone.id == milestone_id
        for task_id in milestone.task_ids
    )
    user_prompt = (
        f"{_render_project_state(state)}\n\n"
        "Current operation context:\n"
        "Operation: IMPACT_ANALYSIS\n"
        "Change request under analysis:\n"
        f"{_render_change_request(request.change_request)}\n"
        f"Existing Requirement IDs: "
        f"{_string_list(tuple(item.id for item in state.requirements))}\n"
        f"Existing Milestone IDs: "
        f"{_string_list(tuple(item.id for item in state.milestones))}\n"
        f"Existing Task IDs: "
        f"{_string_list(tuple(item.id for item in state.tasks))}\n"
        f"Active Plan Task IDs: {_string_list(active_task_ids)}\n"
        "Every affected, reopen, cancel, supersedes, dependency-change, and "
        "milestone task reference must use an exact ID from the snapshot or an "
        "exact new ID declared in this proposal. Never put titles, explanations, "
        "or other natural language in ID fields. Historical Requirement, "
        "Milestone, and Task IDs may be used only in reference, reuse, or update-"
        "target fields. Every *_to_add item and every new entity proposal must "
        "use a fresh ID that does not collide with any historical ProjectState "
        "entity. Put still-valid existing Milestone IDs in milestone_ids_reused; "
        "do not redeclare them in milestones. The milestones field contains only "
        "genuinely new Milestone proposals with fresh IDs. Example — if M1 "
        "exists, incorrect new milestone id: M1. Correct reuse: reference M1 in "
        "milestone_ids_reused. Correct new milestone: use a fresh ID such as M2. "
        "Preserve completed work unless "
        "tasks_to_reopen explicitly names its Task ID. Put added Requirements in "
        "requirements_to_add and replacements in requirements_to_update. The "
        "reused and new Milestones together must place every Task retained by the "
        "replacement Plan exactly once. Return an impact and replan proposal "
        "only; do not modify requirements, tasks, milestones, or plans."
    )
    return _system_prompt(SupervisorOperation.IMPACT_ANALYSIS), user_prompt


def build_progress_report_prompt(
    request: ProgressReportRequest,
) -> tuple[str, str]:
    question = (
        "default project progress report"
        if request.question is None
        else request.question
    )
    user_prompt = (
        f"{_render_project_state(request.project_state)}\n\n"
        "Current operation context:\n"
        "Operation: PROGRESS_REPORT\n"
        f"Question: {_text(question)}\n"
        "Report only facts supported by this snapshot; identify unknown evidence."
    )
    return _system_prompt(SupervisorOperation.PROGRESS_REPORT), user_prompt


__all__ = [
    "SUPERVISOR_SYSTEM_POLICY",
    "build_impact_analysis_prompt",
    "build_plan_prompt",
    "build_progress_report_prompt",
    "build_review_prompt",
]
