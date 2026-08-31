from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

from scripts import manual_deepseek_supervisor_e2e as manual_e2e


class ManualDeepSeekSupervisorE2ETests(unittest.TestCase):
    @patch.object(manual_e2e, "OpenAI")
    @patch.object(manual_e2e, "DefaultHttpx2Client")
    def test_client_construction_is_direct_and_keeps_tls_defaults(
        self,
        default_http_client,
        openai_client,
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
        self.assertNotIn("verify", openai_client.call_args.kwargs)

    @patch.object(manual_e2e, "SupervisorService")
    @patch.object(manual_e2e, "_build_compatibility_client")
    @patch.object(manual_e2e.os, "getenv")
    def test_main_passes_key_to_composition_helper_without_printing_it(
        self,
        getenv,
        build_client,
        supervisor_service,
    ):
        secret = "secret-value-that-must-not-be-printed"
        getenv.side_effect = lambda name: {
            "DEEPSEEK_API_KEY": secret,
            "CODE_MULE_DEEPSEEK_MODEL": "deepseek-model",
        }[name]
        build_client.return_value = object()
        supervisor_service.return_value.report_progress.return_value = "typed report"
        output = StringIO()

        with redirect_stdout(output):
            result = manual_e2e.main()

        self.assertEqual(result, 0)
        build_client.assert_called_once_with(secret)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("REAL DEEPSEEK API CALL — MANUAL ONLY", output.getvalue())


if __name__ == "__main__":
    unittest.main()
