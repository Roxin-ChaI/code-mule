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
    SupervisorCallFailure,
    SupervisorFailureCategory,
    SupervisorRetryPolicy,
)
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
from code_mule.progress import ProgressEventType, RecordingProgressSink

from state import make_project_state
from supervisor.test_parsing import impact_payload, plan_payload, progress_payload


class FakeSupervisorModelClient:
    def __init__(self, payload):
        self.outcomes = list(payload) if isinstance(payload, list) else [payload]
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
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TypedProviderFailure(RuntimeError):
    def __init__(self, category):
        super().__init__("safe provider failure")
        self.failure_category = category


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

    def test_malformed_first_response_regenerates_complete_plan(self):
        state = make_project_state()
        client = FakeSupervisorModelClient([[], plan_payload()])
        service = SupervisorService(client)

        result = service.plan(PlanRequest(state, "Plan"))

        self.assertIsInstance(result, PlanProposal)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("fresh complete response", client.calls[1]["user_prompt"])
        self.assertEqual(len(service.last_attempt_results), 2)

    def test_extra_field_is_not_repaired_and_fresh_response_succeeds(self):
        state = make_project_state()
        invalid = plan_payload()
        invalid["not_used"] = "must-not-leak"
        client = FakeSupervisorModelClient([invalid, plan_payload()])
        service = SupervisorService(client)

        service.plan(PlanRequest(state, "Plan"))

        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("must-not-leak", client.calls[1]["user_prompt"])
        self.assertIs(
            service.last_attempt_results[0].failure_category,
            SupervisorFailureCategory.SCHEMA_CONTRACT_VIOLATION,
        )

    def test_review_illegal_combination_regenerates_and_succeeds(self):
        state = make_project_state()
        invalid = review_payload()
        invalid.update(
            decision="human_required",
            next_task_prompt="unsafe worker instruction",
        )
        client = FakeSupervisorModelClient([invalid, review_payload()])
        service = SupervisorService(client)

        result = service.review(
            ReviewRequest(state, state.tasks[0], state.execution_reports[0])
        )

        self.assertIsInstance(result, ReviewResult)
        self.assertEqual(len(client.calls), 2)
        self.assertIs(
            service.last_attempt_results[0].failure_category,
            SupervisorFailureCategory.DECISION_CONTRACT_VIOLATION,
        )

    def test_retry_exhaustion_obeys_max_attempts_and_emits_safe_progress(self):
        state = make_project_state()
        client = FakeSupervisorModelClient(
            [{"invalid": "secret-one"}, {"invalid": "secret-two"}]
        )
        progress = RecordingProgressSink()
        sleeps = []
        service = SupervisorService(
            client,
            retry_policy=SupervisorRetryPolicy(
                max_attempts=2,
                retry_delay_seconds=0.25,
            ),
            sleeper=sleeps.append,
            progress_sink=progress,
        )

        with self.assertRaises(SupervisorCallFailure) as raised:
            service.plan(PlanRequest(state, "Plan"))

        failure = raised.exception
        self.assertEqual(failure.attempt_count, 2)
        self.assertTrue(failure.retryable)
        self.assertTrue(failure.exhausted)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(
            tuple(event.type for event in progress.events),
            (
                ProgressEventType.SUPERVISOR_RETRYING,
                ProgressEventType.SUPERVISOR_RETRY_EXHAUSTED,
            ),
        )
        self.assertNotIn("secret-one", repr(progress.events))
        self.assertNotIn("secret-two", str(failure))

    def test_timeout_is_retryable_but_content_filter_and_auth_are_not(self):
        state = make_project_state()
        timeout_client = FakeSupervisorModelClient(
            [TimeoutError("temporary"), plan_payload()]
        )
        SupervisorService(timeout_client).plan(PlanRequest(state, "Plan"))
        self.assertEqual(len(timeout_client.calls), 2)

        for category in (
            SupervisorFailureCategory.CONTENT_FILTER,
            SupervisorFailureCategory.PROVIDER_AUTHENTICATION,
            SupervisorFailureCategory.PROVIDER_CONFIGURATION,
        ):
            with self.subTest(category=category):
                client = FakeSupervisorModelClient(
                    [TypedProviderFailure(category), plan_payload()]
                )
                with self.assertRaises(SupervisorCallFailure) as raised:
                    SupervisorService(client).plan(
                        PlanRequest(state, "Plan")
                    )
                self.assertIs(raised.exception.failure_category, category)
                self.assertEqual(raised.exception.attempt_count, 1)
                self.assertEqual(len(client.calls), 1)

    def test_plan_review_impact_and_progress_all_share_bounded_layer(self):
        state = make_project_state()
        cases = (
            (
                lambda service: service.plan(PlanRequest(state, "Plan")),
                plan_payload(),
                SupervisorOperation.PLAN,
            ),
            (
                lambda service: service.review(
                    ReviewRequest(
                        state, state.tasks[0], state.execution_reports[0]
                    )
                ),
                review_payload(),
                SupervisorOperation.REVIEW,
            ),
            (
                lambda service: service.analyze_change(
                    ImpactAnalysisRequest(state, state.change_requests[0])
                ),
                impact_payload(),
                SupervisorOperation.IMPACT_ANALYSIS,
            ),
            (
                lambda service: service.report_progress(
                    ProgressReportRequest(state, None)
                ),
                progress_payload(),
                SupervisorOperation.PROGRESS_REPORT,
            ),
        )
        for call, valid, operation in cases:
            with self.subTest(operation=operation):
                client = FakeSupervisorModelClient([["invalid"], valid])
                service = SupervisorService(client)
                call(service)
                self.assertEqual(len(client.calls), 2)
                self.assertTrue(
                    all(
                        item["operation"] is operation
                        for item in client.calls
                    )
                )

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
