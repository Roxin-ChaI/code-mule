import unittest
from dataclasses import replace

from code_mule.supervisor.contracts import (
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
from code_mule.supervisor.parsing import InvalidSupervisorResponse
from code_mule.supervisor.prompts import (
    build_impact_analysis_prompt,
    build_plan_prompt,
    build_progress_report_prompt,
    build_review_prompt,
)
from code_mule.supervisor.schemas import (
    impact_analysis_response_schema,
    plan_response_schema,
    progress_report_response_schema,
    review_response_schema,
)
from code_mule.supervisor.service import SupervisorService

from state import make_project_state
from supervisor.test_parsing import impact_payload, plan_payload, progress_payload


class FakeSupervisorModelClient:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def create_structured_response(
        self,
        *,
        operation: SupervisorOperation,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(
            {
                "operation": operation,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema": schema,
            }
        )
        return self.payload


def review_payload():
    return {
        "decision": "continue",
        "rationale": "evidence passed",
        "next_task_prompt": None,
        "issues": [],
    }


class SupervisorServiceTests(unittest.TestCase):
    def test_plan_calls_client_once_with_protocol_and_returns_typed_result(self):
        state = make_project_state()
        original = replace(state)
        request = PlanRequest(state, "Plan Phase 4")
        client = FakeSupervisorModelClient(plan_payload())
        service = SupervisorService(client)

        result = service.plan(request)

        self.assertIsInstance(result, PlanProposal)
        self._assert_single_call(
            client,
            SupervisorOperation.PLAN,
            build_plan_prompt(request),
            plan_response_schema(),
        )
        self.assertEqual(state, original)

    def test_review_calls_client_once_with_protocol_and_returns_typed_result(self):
        state = make_project_state()
        original = replace(state)
        request = ReviewRequest(state, state.tasks[0], state.execution_reports[0])
        client = FakeSupervisorModelClient(review_payload())
        service = SupervisorService(client)

        result = service.review(request)

        self.assertIsInstance(result, ReviewResult)
        self._assert_single_call(
            client,
            SupervisorOperation.REVIEW,
            build_review_prompt(request),
            review_response_schema(),
        )
        self.assertEqual(state, original)

    def test_analyze_change_calls_client_once_and_returns_typed_result(self):
        state = make_project_state()
        original = replace(state)
        request = ImpactAnalysisRequest(state, state.change_requests[0])
        client = FakeSupervisorModelClient(impact_payload())
        service = SupervisorService(client)

        result = service.analyze_change(request)

        self.assertIsInstance(result, ImpactAnalysisResult)
        self._assert_single_call(
            client,
            SupervisorOperation.IMPACT_ANALYSIS,
            build_impact_analysis_prompt(request),
            impact_analysis_response_schema(),
        )
        self.assertEqual(state, original)

    def test_report_progress_calls_client_once_and_returns_typed_result(self):
        state = make_project_state()
        original = replace(state)
        request = ProgressReportRequest(state, None)
        client = FakeSupervisorModelClient(progress_payload())
        service = SupervisorService(client)

        result = service.report_progress(request)

        self.assertIsInstance(result, ProgressReport)
        self._assert_single_call(
            client,
            SupervisorOperation.PROGRESS_REPORT,
            build_progress_report_prompt(request),
            progress_report_response_schema(),
        )
        self.assertEqual(state, original)

    def test_invalid_response_is_not_retried_or_repaired(self):
        state = make_project_state()
        client = FakeSupervisorModelClient({"invalid": True})
        service = SupervisorService(client)

        with self.assertRaises(InvalidSupervisorResponse):
            service.plan(PlanRequest(state, "Plan"))

        self.assertEqual(len(client.calls), 1)

    def _assert_single_call(
        self,
        client: FakeSupervisorModelClient,
        operation: SupervisorOperation,
        prompts: tuple[str, str],
        schema: dict[str, object],
    ) -> None:
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertIs(call["operation"], operation)
        self.assertEqual(call["system_prompt"], prompts[0])
        self.assertEqual(call["user_prompt"], prompts[1])
        self.assertEqual(call["schema"], schema)


if __name__ == "__main__":
    unittest.main()
