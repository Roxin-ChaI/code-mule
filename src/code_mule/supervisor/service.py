"""Supervisor reasoning service with bounded, provider-neutral regeneration."""

from collections.abc import Callable
from datetime import UTC, datetime
import time
from typing import TypeVar

from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)

from .client import SupervisorModelClient
from .contracts import (
    ImpactAnalysisRequest,
    ImpactAnalysisResult,
    PlanProposal,
    PlanRequest,
    ProgressReport,
    ProgressReportRequest,
    ReviewRequest,
    ReviewResult,
    SupervisorAttemptResult,
    SupervisorCallFailure,
    SupervisorFailureCategory,
    SupervisorOperation,
    SupervisorRetryPolicy,
    supervisor_failure_is_retryable,
    FinalReviewRequest,
    FinalReviewResult,
)
from .parsing import (
    InvalidSupervisorResponse,
    parse_impact_analysis_response,
    parse_plan_response,
    parse_progress_report_response,
    parse_review_response,
    parse_final_review_response,
)
from .prompts import (
    build_impact_analysis_prompt,
    build_plan_prompt,
    build_progress_report_prompt,
    build_review_prompt,
    build_final_review_prompt,
)
from .schemas import (
    impact_analysis_response_schema,
    plan_response_schema,
    progress_report_response_schema,
    review_response_schema,
    final_review_response_schema,
)


_Result = TypeVar("_Result")


class SupervisorService:
    """Convert domain requests into validated results with bounded regeneration."""

    def __init__(
        self,
        client: SupervisorModelClient,
        *,
        retry_policy: SupervisorRetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        progress_sink: ProgressSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._retry_policy = retry_policy or SupervisorRetryPolicy()
        self._sleeper = sleeper
        self._progress = resilient_progress_sink(progress_sink)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_attempt_results: tuple[SupervisorAttemptResult, ...] = ()

    @property
    def last_attempt_results(self) -> tuple[SupervisorAttemptResult, ...]:
        return self._last_attempt_results

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def plan(self, request: PlanRequest) -> PlanProposal:
        return self._execute(
            operation=SupervisorOperation.PLAN,
            prompts=build_plan_prompt(request),
            schema_factory=plan_response_schema,
            parser=parse_plan_response,
            project_id=request.project_state.project.id,
            task_id=None,
        )

    def review(self, request: ReviewRequest) -> ReviewResult:
        return self._execute(
            operation=SupervisorOperation.REVIEW,
            prompts=build_review_prompt(request),
            schema_factory=review_response_schema,
            parser=parse_review_response,
            project_id=request.project_state.project.id,
            task_id=request.task.id,
        )

    def analyze_change(
        self,
        request: ImpactAnalysisRequest,
    ) -> ImpactAnalysisResult:
        return self._execute(
            operation=SupervisorOperation.IMPACT_ANALYSIS,
            prompts=build_impact_analysis_prompt(request),
            schema_factory=impact_analysis_response_schema,
            parser=parse_impact_analysis_response,
            project_id=request.project_state.project.id,
            task_id=None,
        )

    def report_progress(self, request: ProgressReportRequest) -> ProgressReport:
        return self._execute(
            operation=SupervisorOperation.PROGRESS_REPORT,
            prompts=build_progress_report_prompt(request),
            schema_factory=progress_report_response_schema,
            parser=parse_progress_report_response,
            project_id=request.project_state.project.id,
            task_id=None,
        )

    def final_review(self, request: FinalReviewRequest) -> FinalReviewResult:
        return self._execute(
            operation=SupervisorOperation.FINAL_REVIEW,
            prompts=build_final_review_prompt(request),
            schema_factory=final_review_response_schema,
            parser=parse_final_review_response,
            project_id=request.project_state.project.id,
            task_id=None,
        )

    def _execute(
        self,
        *,
        operation: SupervisorOperation,
        prompts: tuple[str, str],
        schema_factory: Callable[[], dict[str, object]],
        parser: Callable[[dict[str, object]], _Result],
        project_id: str,
        task_id: str | None,
    ) -> _Result:
        system_prompt, original_user_prompt = prompts
        attempts: tuple[SupervisorAttemptResult, ...] = ()
        self._last_attempt_results = ()
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            user_prompt = (
                original_user_prompt
                if attempt == 1
                else self._regeneration_prompt(original_user_prompt, operation)
            )
            try:
                payload = self._client.create_structured_response(
                    operation=operation,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema=schema_factory(),
                )
                result = parser(payload)
            except Exception as error:
                category = _failure_category(error)
                retryable = supervisor_failure_is_retryable(category)
                attempts += (
                    SupervisorAttemptResult(
                        operation,
                        attempt,
                        False,
                        category,
                        retryable,
                    ),
                )
                self._last_attempt_results = attempts
                if retryable and attempt < self._retry_policy.max_attempts:
                    next_attempt = attempt + 1
                    self._emit(
                        ProgressEventType.SUPERVISOR_RETRYING,
                        f"Supervisor response invalid. Regenerating "
                        f"({next_attempt}/{self._retry_policy.max_attempts})...",
                        operation,
                        next_attempt,
                        category,
                        project_id,
                        task_id,
                    )
                    if self._retry_policy.retry_delay_seconds:
                        self._sleeper(
                            self._retry_policy.retry_delay_seconds
                        )
                    continue
                exhausted = (
                    retryable
                    and attempt >= self._retry_policy.max_attempts
                )
                if exhausted:
                    self._emit(
                        ProgressEventType.SUPERVISOR_RETRY_EXHAUSTED,
                        "Supervisor regeneration attempts exhausted.",
                        operation,
                        attempt,
                        category,
                        project_id,
                        task_id,
                    )
                raise SupervisorCallFailure(
                    operation=operation,
                    failure_category=category,
                    attempt_count=attempt,
                    retryable=retryable,
                    exhausted=exhausted,
                ) from error

            attempts += (
                SupervisorAttemptResult(
                    operation,
                    attempt,
                    True,
                    None,
                    False,
                ),
            )
            self._last_attempt_results = attempts
            if attempt > 1:
                self._emit(
                    ProgressEventType.SUPERVISOR_RETRY_SUCCEEDED,
                    "Supervisor regeneration succeeded.",
                    operation,
                    attempt,
                    attempts[-2].failure_category,
                    project_id,
                    task_id,
                )
            return result
        raise AssertionError("Supervisor retry loop did not return or raise")

    @staticmethod
    def _regeneration_prompt(
        original_user_prompt: str,
        operation: SupervisorOperation,
    ) -> str:
        return (
            f"{original_user_prompt}\n\n"
            "Regeneration instruction:\n"
            f"Previous response violated the required {operation.name} contract. "
            "Return a fresh complete response matching the schema. Do not repair "
            "or reuse the previous response."
        )

    def _emit(
        self,
        event_type: ProgressEventType,
        message: str,
        operation: SupervisorOperation,
        attempt: int,
        category: SupervisorFailureCategory | None,
        project_id: str,
        task_id: str | None,
    ) -> None:
        metadata = {
            "operation": operation.value,
            "attempt": str(attempt),
        }
        if category is not None:
            metadata["failure_category"] = category.value
        self._progress.emit(
            ProgressEvent(
                type=event_type,
                timestamp=self._clock(),
                project_id=project_id,
                task_id=task_id,
                attempt=attempt,
                message=message,
                metadata=metadata,
            )
        )


def _failure_category(error: Exception) -> SupervisorFailureCategory:
    category = getattr(error, "failure_category", None)
    if isinstance(category, SupervisorFailureCategory):
        return category
    if isinstance(error, TimeoutError):
        return SupervisorFailureCategory.TRANSPORT_TIMEOUT
    if isinstance(error, ConnectionError):
        return SupervisorFailureCategory.TEMPORARY_CONNECTION_FAILURE
    if isinstance(error, InvalidSupervisorResponse):
        return error.failure_category
    return SupervisorFailureCategory.UNKNOWN_FAILURE


__all__ = ["SupervisorService"]
