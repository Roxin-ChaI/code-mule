import copy
import inspect
from types import SimpleNamespace
import unittest

from code_mule.supervisor.contracts import SupervisorOperation
from code_mule.supervisor.providers import deepseek as provider_module
from code_mule.supervisor.providers.deepseek import (
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
    DeepSeekSupervisorResponseError,
)


class FakeResponsesAPI:
    def __init__(self, response=None, *, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeDeepSeekClient:
    def __init__(self, responses: FakeResponsesAPI):
        self.responses = responses


def make_provider(
    response=None,
    *,
    error: Exception | None = None,
    model: str = "explicit-model",
    max_output_tokens: int | None = None,
):
    responses = FakeResponsesAPI(response, error=error)
    client = FakeDeepSeekClient(responses)
    provider = DeepSeekSupervisorModelClient(
        client,
        DeepSeekSupervisorConfig(model, max_output_tokens),
    )
    return provider, responses


class DeepSeekSupervisorConfigTests(unittest.TestCase):
    def test_config_preserves_explicit_values_without_stripping(self):
        config = DeepSeekSupervisorConfig(" explicit-model ", 123)
        self.assertEqual(config.model, " explicit-model ")
        self.assertEqual(config.max_output_tokens, 123)

    def test_empty_model_is_rejected(self):
        with self.assertRaises(ValueError):
            DeepSeekSupervisorConfig("")

    def test_invalid_max_output_tokens_are_rejected(self):
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DeepSeekSupervisorConfig("model", value)


class DeepSeekSupervisorRequestTests(unittest.TestCase):
    def test_request_fidelity_and_schema_isolation_for_all_operations(self):
        expected_names = {
            SupervisorOperation.PLAN: "code_mule_plan",
            SupervisorOperation.REVIEW: "code_mule_review",
            SupervisorOperation.IMPACT_ANALYSIS: "code_mule_impact_analysis",
            SupervisorOperation.PROGRESS_REPORT: "code_mule_progress_report",
        }
        for operation, expected_name in expected_names.items():
            with self.subTest(operation=operation):
                response = SimpleNamespace(status="completed", output_text='{"ok": [2, 1]}')
                provider, responses = make_provider(response)
                schema = {
                    "type": "object",
                    "properties": {"ok": {"type": "array"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
                original_schema = copy.deepcopy(schema)

                result = provider.create_structured_response(
                    operation=operation,
                    system_prompt="system policy",
                    user_prompt="user state",
                    schema=schema,
                )

                self.assertEqual(result, {"ok": [2, 1]})
                self.assertEqual(len(responses.calls), 1)
                request = responses.calls[0]
                self.assertEqual(request["model"], "explicit-model")
                self.assertEqual(request["instructions"], "system policy")
                self.assertEqual(request["input"], "user state")
                self.assertNotIn("max_output_tokens", request)
                for forbidden in (
                    "tools",
                    "conversation",
                    "previous_response_id",
                    "stream",
                    "background",
                    "store",
                ):
                    self.assertNotIn(forbidden, request)
                response_format = request["text"]["format"]
                self.assertEqual(response_format["type"], "json_schema")
                self.assertEqual(response_format["name"], expected_name)
                self.assertNotIn("strict", response_format)
                self.assertEqual(response_format["schema"], original_schema)
                self.assertIsNot(response_format["schema"], schema)
                self.assertEqual(schema, original_schema)

    def test_configured_max_output_tokens_is_preserved(self):
        response = SimpleNamespace(status="completed", output_text="{}")
        provider, responses = make_provider(response, max_output_tokens=321)
        provider.create_structured_response(
            operation=SupervisorOperation.PLAN,
            system_prompt="system",
            user_prompt="user",
            schema={},
        )
        self.assertEqual(responses.calls[0]["max_output_tokens"], 321)

    def test_provider_module_does_not_read_environment_or_contain_secret_config(self):
        source = inspect.getsource(provider_module)
        self.assertNotIn("getenv", source)
        self.assertNotIn("environ", source)
        self.assertNotIn("DEEPSEEK_API_KEY", source)
        self.assertNotIn("api_key", source)


class DeepSeekSupervisorResponseTests(unittest.TestCase):
    def test_success_returns_plain_dict_and_preserves_array_order(self):
        response = SimpleNamespace(
            status="completed",
            output_text='{"decision":"continue","issues":["second","first"]}',
        )
        provider, _ = make_provider(response)
        result = provider.create_structured_response(
            operation=SupervisorOperation.REVIEW,
            system_prompt="system",
            user_prompt="user",
            schema={},
        )
        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["issues"], ["second", "first"])

    def test_invalid_json_is_wrapped(self):
        provider, responses = make_provider(
            SimpleNamespace(status="completed", output_text="not json")
        )
        with self.assertRaises(DeepSeekSupervisorResponseError) as context:
            self._call(provider)
        self.assertNotIsInstance(context.exception, ValueError)
        self.assertEqual(len(responses.calls), 1)

    def test_non_object_json_is_rejected(self):
        for output_text in ('["array"]', '"string"', "null"):
            with self.subTest(output_text=output_text):
                provider, responses = make_provider(
                    SimpleNamespace(status="completed", output_text=output_text)
                )
                with self.assertRaises(DeepSeekSupervisorResponseError):
                    self._call(provider)
                self.assertEqual(len(responses.calls), 1)

    def test_empty_or_missing_output_text_is_rejected(self):
        responses_to_test = (
            SimpleNamespace(status="completed", output_text=""),
            SimpleNamespace(status="completed"),
            SimpleNamespace(status="completed", output_text=None),
        )
        for response in responses_to_test:
            with self.subTest(response=response):
                provider, calls = make_provider(response)
                with self.assertRaises(DeepSeekSupervisorResponseError):
                    self._call(provider)
                self.assertEqual(len(calls.calls), 1)

    def test_non_completed_statuses_are_rejected_with_status_in_message(self):
        for status in ("failed", "incomplete", "in_progress", "unknown"):
            with self.subTest(status=status):
                provider, responses = make_provider(
                    SimpleNamespace(status=status, output_text="{}")
                )
                with self.assertRaises(DeepSeekSupervisorResponseError) as context:
                    self._call(provider)
                self.assertIn(status, str(context.exception))
                self.assertEqual(len(responses.calls), 1)

    def test_underlying_client_exception_propagates_without_retry(self):
        failure = RuntimeError("transport failed")
        provider, responses = make_provider(error=failure)
        with self.assertRaises(RuntimeError) as context:
            self._call(provider)
        self.assertIs(context.exception, failure)
        self.assertEqual(len(responses.calls), 1)

    def _call(self, provider: DeepSeekSupervisorModelClient) -> dict[str, object]:
        return provider.create_structured_response(
            operation=SupervisorOperation.PLAN,
            system_prompt="system",
            user_prompt="user",
            schema={},
        )


if __name__ == "__main__":
    unittest.main()
