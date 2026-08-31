"""Conservative mapping from structured Codex results to domain reports."""

from datetime import datetime

from code_mule.domain.models import ExecutionReport

from .contracts import WorkerTaskRequest, WorkerTurnResult


_NO_FINAL_MESSAGE = "Codex turn completed without a final agent message."


def build_execution_report(
    request: WorkerTaskRequest,
    result: WorkerTurnResult,
    report_id: str,
    created_at: datetime,
) -> ExecutionReport:
    """Build a report without inferring evidence from the agent's prose."""

    if report_id == "":
        raise ValueError("report_id must not be empty")
    return ExecutionReport(
        id=report_id,
        task_id=request.task.id,
        attempt=request.task.execution_attempts + 1,
        status="completed" if result.completed else "failed",
        files_changed=(),
        tests=(),
        static_checks=(),
        git_state="unknown",
        issues=result.issues,
        human_action_required=False,
        summary=result.final_message or _NO_FINAL_MESSAGE,
        created_at=created_at,
    )


__all__ = ["build_execution_report"]
