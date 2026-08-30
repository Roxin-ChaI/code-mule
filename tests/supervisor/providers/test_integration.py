import json
from types import SimpleNamespace
import unittest

from code_mule.domain.enums import SupervisorDecisionType
from code_mule.supervisor.contracts import ReviewRequest
from code_mule.supervisor.providers.openai import (
    OpenAISupervisorConfig,
    OpenAISupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService

from state import make_project_state


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


if __name__ == "__main__":
    unittest.main()
