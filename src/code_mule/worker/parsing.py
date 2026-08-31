"""Deterministic mapping from validated Codex evidence to domain reports."""

from datetime import datetime

from code_mule.domain.models import ExecutionReport

from .contracts import WorkerTaskRequest
from .structured_report import StructuredWorkerReport, WorkerCheckResult


def build_execution_report(
    request: WorkerTaskRequest,
    result: StructuredWorkerReport,
    report_id: str,
    created_at: datetime,
    transport_issues: tuple[str, ...] = (),
) -> ExecutionReport:
    """Map only fields already validated by the structured report parser."""

    if report_id == "":
        raise ValueError("report_id must not be empty")
    return ExecutionReport(
        id=report_id,
        task_id=request.task.id,
        attempt=request.task.execution_attempts + 1,
        status=result.status.value,
        files_changed=result.files_changed,
        tests=tuple(_format_check(check) for check in result.tests),
        static_checks=tuple(_format_check(check) for check in result.static_checks),
        git_state=result.git_state,
        issues=transport_issues + result.issues,
        human_action_required=result.human_action_required,
        summary=result.summary,
        created_at=created_at,
    )


def _format_check(check: WorkerCheckResult) -> str:
    formatted = f"{check.name}: {check.status.value}"
    if check.detail is not None:
        return f"{formatted} ({check.detail})"
    return formatted


__all__ = ["build_execution_report"]
