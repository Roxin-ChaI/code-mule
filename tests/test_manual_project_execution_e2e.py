from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

from scripts import manual_project_execution_e2e as manual_e2e


class ManualProjectExecutionE2ETests(unittest.TestCase):
    def test_manual_correctness_e2e_does_not_set_output_token_cap(self):
        config = manual_e2e._build_supervisor_config("explicit-model")
        self.assertEqual(config.model, "explicit-model")
        self.assertIsNone(config.max_output_tokens)

    @patch.object(manual_e2e, "OpenAI")
    @patch.object(manual_e2e, "DefaultHttpx2Client")
    def test_client_ignores_ambient_proxy_without_disabling_tls(
        self, default_http_client, openai_client
    ):
        transport = object()
        client = object()
        default_http_client.return_value = transport
        openai_client.return_value = client

        result = manual_e2e._build_compatibility_client("diagnostic-key")

        self.assertIs(result, client)
        default_http_client.assert_called_once_with(trust_env=False)
        openai_client.assert_called_once_with(
            api_key="diagnostic-key",
            base_url="https://api.deepseek.com",
            max_retries=0,
            http_client=transport,
        )
        self.assertNotIn("verify", default_http_client.call_args.kwargs)

    @patch.object(manual_e2e.os, "getenv", return_value=None)
    def test_missing_key_stops_before_real_composition(self, getenv):
        output = StringIO()
        errors = StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = manual_e2e.main()
        self.assertEqual(result, 2)
        self.assertIn(
            "REAL DEEPSEEK + CODEX MULTI-TASK PROJECT — MANUAL ONLY",
            output.getvalue(),
        )
        self.assertIn("real DeepSeek API calls", output.getvalue())
        self.assertIn("real locally authenticated Codex", output.getvalue())
        self.assertIn("disposable temporary Git repository", output.getvalue())
        self.assertIn("DEEPSEEK_API_KEY is required", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
