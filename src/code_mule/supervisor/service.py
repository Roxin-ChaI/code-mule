"""Supervisor reasoning service over a provider-neutral structured client."""

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
    SupervisorOperation,
)
from .parsing import (
    parse_impact_analysis_response,
    parse_plan_response,
    parse_progress_report_response,
    parse_review_response,
)
from .prompts import (
    build_impact_analysis_prompt,
    build_plan_prompt,
    build_progress_report_prompt,
    build_review_prompt,
)
from .schemas import (
    impact_analysis_response_schema,
    plan_response_schema,
    progress_report_response_schema,
    review_response_schema,
)


class SupervisorService:
    """Convert domain requests into validated, typed reasoning results."""

    def __init__(self, client: SupervisorModelClient):
        self._client = client

    def plan(self, request: PlanRequest) -> PlanProposal:
        system_prompt, user_prompt = build_plan_prompt(request)
        payload = self._client.create_structured_response(
            operation=SupervisorOperation.PLAN,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=plan_response_schema(),
        )
        return parse_plan_response(payload)

    def review(self, request: ReviewRequest) -> ReviewResult:
        system_prompt, user_prompt = build_review_prompt(request)
        payload = self._client.create_structured_response(
            operation=SupervisorOperation.REVIEW,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=review_response_schema(),
        )
        return parse_review_response(payload)

    def analyze_change(
        self,
        request: ImpactAnalysisRequest,
    ) -> ImpactAnalysisResult:
        system_prompt, user_prompt = build_impact_analysis_prompt(request)
        payload = self._client.create_structured_response(
            operation=SupervisorOperation.IMPACT_ANALYSIS,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=impact_analysis_response_schema(),
        )
        return parse_impact_analysis_response(payload)

    def report_progress(self, request: ProgressReportRequest) -> ProgressReport:
        system_prompt, user_prompt = build_progress_report_prompt(request)
        payload = self._client.create_structured_response(
            operation=SupervisorOperation.PROGRESS_REPORT,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=progress_report_response_schema(),
        )
        return parse_progress_report_response(payload)


__all__ = ["SupervisorService"]
