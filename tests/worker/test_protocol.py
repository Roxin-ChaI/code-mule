import unittest

from code_mule.worker.contracts import CodexProtocolError
from code_mule.worker.protocol import (
    MessageKind,
    classify_message,
    notification_message,
    request_message,
    response_result,
)


class ProtocolHelperTests(unittest.TestCase):
    def test_builders_match_app_server_jsonl_envelopes(self):
        self.assertEqual(
            request_message(1, "initialize", {"clientInfo": {"name": "code-mule"}}),
            {
                "method": "initialize",
                "id": 1,
                "params": {"clientInfo": {"name": "code-mule"}},
            },
        )
        self.assertEqual(notification_message("initialized"), {"method": "initialized"})

    def test_classifies_response_notification_and_server_request(self):
        self.assertIs(classify_message({"id": 1, "result": {}}), MessageKind.RESPONSE)
        self.assertIs(
            classify_message({"method": "turn/completed", "params": {}}),
            MessageKind.NOTIFICATION,
        )
        self.assertIs(
            classify_message(
                {"id": 5, "method": "item/tool/requestUserInput", "params": {}}
            ),
            MessageKind.SERVER_REQUEST,
        )

    def test_invalid_envelopes_fail_closed(self):
        invalid = (
            [],
            {},
            {"id": 1},
            {"result": {}},
            {"id": 1, "result": {}, "error": {}},
            {"id": 1, "method": 42, "params": {}},
        )
        for message in invalid:
            with self.subTest(message=message):
                with self.assertRaises(CodexProtocolError):
                    classify_message(message)

    def test_response_validation_checks_id_error_and_object_result(self):
        self.assertEqual(response_result({"id": 2, "result": {"ok": True}}, 2), {"ok": True})
        for message in (
            {"id": 3, "result": {}},
            {"id": 2, "error": {"message": "no"}},
            {"id": 2, "result": "bad"},
        ):
            with self.subTest(message=message):
                with self.assertRaises(CodexProtocolError):
                    response_result(message, 2)


if __name__ == "__main__":
    unittest.main()
