import json
from types import SimpleNamespace
import unittest

from code_mule.domain.enums import SupervisorDecisionType
from code_mule.supervisor.contracts import (
    PlanProposal,
    PlanRequest,
    ProgressReport,
    ProgressReportRequest,
    ReviewRequest,
)
from code_mule.supervisor.providers.deepseek import (
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
)
from code_mule.supervisor.providers.openai import (
    OpenAISupervisorConfig,
    OpenAISupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService
from code_mule.supervisor.schemas import plan_response_schema

from state import make_project_state
from supervisor.test_parsing import plan_payload


class IntegrationFakeResponsesAPI:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output_text=json.dumps(
                {
                    "decision": "continue",
                    "rationale": "recorded verification passed",
                    "next_task_prompt": None,
                    "issues": ["issue-b", "issue-a"],
                }
            ),
        )


class IntegrationFakeOpenAIClient:
    def __init__(self):
        self.responses = IntegrationFakeResponsesAPI()


class OpenAISupervisorIntegrationTests(unittest.TestCase):
    def test_review_crosses_provider_and_local_parser_boundaries(self):
        state = make_project_state()
        client = IntegrationFakeOpenAIClient()
        provider = OpenAISupervisorModelClient(
            client,
            OpenAISupervisorConfig("integration-model", 500),
        )
        service = SupervisorService(provider)
        request = ReviewRequest(
            state,
            state.tasks[0],
            state.execution_reports[0],
        )

        result = service.review(request)

        self.assertIs(result.decision, SupervisorDecisionType.CONTINUE)
        self.assertEqual(result.issues, ("issue-b", "issue-a"))
        self.assertEqual(len(client.responses.calls), 1)
        api_request = client.responses.calls[0]
        self.assertEqual(api_request["model"], "integration-model")
        self.assertEqual(api_request["max_output_tokens"], 500)
        response_format = api_request["text"]["format"]
        self.assertEqual(response_format["name"], "code_mule_review")
        self.assertIs(response_format["strict"], True)
        self.assertEqual(response_format["schema"]["additionalProperties"], False)
        self.assertEqual(state, request.project_state)


class DeepSeekIntegrationFakeResponsesAPI:
    def __init__(self, payload=None):
        self.calls: list[dict[str, object]] = []
        self.payload = payload or {
            "summary": "Phase 5 correction is running",
            "current_status": "running",
            "current_work": "DeepSeek provider",
            "completed": ["OpenAI provider retained"],
            "remaining": ["manual DeepSeek E2E"],
            "blockers": [],
            "risks": ["real E2E not yet run"],
            "quality_summary": "automated boundary verified",
        }

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output_text=json.dumps(self.payload),
        )


class DeepSeekIntegrationFakeClient:
    def __init__(self, payload=None):
        self.responses = DeepSeekIntegrationFakeResponsesAPI(payload)


class DeepSeekSupervisorIntegrationTests(unittest.TestCase):
    def test_plan_uses_the_exact_plan_json_schema_and_parser_contract(self):
        state = make_project_state()
        client = DeepSeekIntegrationFakeClient(plan_payload())
        provider = DeepSeekSupervisorModelClient(
            client,
            DeepSeekSupervisorConfig("deepseek-integration-model"),
        )
        service = SupervisorService(provider)

        result = service.plan(PlanRequest(state, "Plan exactly"))

        self.assertIsInstance(result, PlanProposal)
        self.assertEqual(len(client.responses.calls), 1)
        api_request = client.responses.calls[0]
        response_format = api_request["text"]["format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["name"], "code_mule_plan")
        self.assertNotIn("strict", response_format)
        self.assertEqual(response_format["schema"], plan_response_schema())
        self.assertIs(response_format["schema"]["additionalProperties"], False)
        self.assertNotIn("not_used", repr(response_format["schema"]))

    def test_progress_report_crosses_provider_and_local_parser_boundaries(self):
        state = make_project_state()
        client = DeepSeekIntegrationFakeClient()
        provider = DeepSeekSupervisorModelClient(
            client,
            DeepSeekSupervisorConfig("deepseek-integration-model", 500),
        )
        service = SupervisorService(provider)
        request = ProgressReportRequest(state, question="What remains?")

        result = service.report_progress(request)

        self.assertIsInstance(result, ProgressReport)
        self.assertEqual(result.current_status, "running")
        self.assertEqual(result.remaining, ("manual DeepSeek E2E",))
        self.assertEqual(len(client.responses.calls), 1)
        api_request = client.responses.calls[0]
        self.assertEqual(api_request["model"], "deepseek-integration-model")
        self.assertEqual(api_request["max_output_tokens"], 500)
        response_format = api_request["text"]["format"]
        self.assertEqual(response_format["name"], "code_mule_progress_report")
        self.assertNotIn("strict", response_format)
        self.assertNotIn("store", api_request)
        self.assertEqual(response_format["schema"]["additionalProperties"], False)
        self.assertEqual(state, request.project_state)


if __name__ == "__main__":
    unittest.main()
