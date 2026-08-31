"""Pure helpers for the Codex app-server JSONL protocol."""

from enum import StrEnum
from typing import cast

from .contracts import CodexProtocolError


INITIALIZE_METHOD = "initialize"
INITIALIZED_METHOD = "initialized"
THREAD_START_METHOD = "thread/start"
TURN_START_METHOD = "turn/start"
ITEM_COMPLETED_METHOD = "item/completed"
TURN_COMPLETED_METHOD = "turn/completed"

APPROVAL_REQUEST_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "applyPatchApproval",
        "execCommandApproval",
    }
)
USER_INPUT_REQUEST_METHODS = frozenset(
    {"item/tool/requestUserInput", "mcpServer/elicitation/request"}
)


class MessageKind(StrEnum):
    RESPONSE = "response"
    NOTIFICATION = "notification"
    SERVER_REQUEST = "server_request"


def request_message(
    request_id: int, method: str, params: dict[str, object]
) -> dict[str, object]:
    if request_id < 0:
        raise ValueError("request_id must be non-negative")
    if method == "":
        raise ValueError("method must not be empty")
    return {"method": method, "id": request_id, "params": params}


def notification_message(
    method: str, params: dict[str, object] | None = None
) -> dict[str, object]:
    if method == "":
        raise ValueError("method must not be empty")
    message: dict[str, object] = {"method": method}
    if params is not None:
        message["params"] = params
    return message


def classify_message(message: object) -> MessageKind:
    if not isinstance(message, dict) or not all(
        isinstance(key, str) for key in message
    ):
        raise CodexProtocolError("app-server message must be an object")
    payload = cast(dict[str, object], message)
    has_id = "id" in payload
    has_method = isinstance(payload.get("method"), str)
    has_result = "result" in payload
    has_error = "error" in payload

    if has_id and has_method and not has_result and not has_error:
        return MessageKind.SERVER_REQUEST
    if has_id and not has_method and has_result != has_error:
        return MessageKind.RESPONSE
    if not has_id and has_method and not has_result and not has_error:
        return MessageKind.NOTIFICATION
    raise CodexProtocolError("app-server message has an invalid envelope")


def response_result(message: object, expected_id: int) -> dict[str, object]:
    if classify_message(message) is not MessageKind.RESPONSE:
        raise CodexProtocolError("expected an app-server response")
    payload = cast(dict[str, object], message)
    if payload["id"] != expected_id:
        raise CodexProtocolError(
            f"response id {payload['id']!r} does not match request {expected_id}"
        )
    if "error" in payload:
        raise CodexProtocolError("app-server returned a structured request error")
    result = payload["result"]
    if not isinstance(result, dict) or not all(
        isinstance(key, str) for key in result
    ):
        raise CodexProtocolError("app-server response result must be an object")
    return cast(dict[str, object], result)


__all__ = [
    "APPROVAL_REQUEST_METHODS",
    "INITIALIZED_METHOD",
    "INITIALIZE_METHOD",
    "ITEM_COMPLETED_METHOD",
    "MessageKind",
    "THREAD_START_METHOD",
    "TURN_COMPLETED_METHOD",
    "TURN_START_METHOD",
    "USER_INPUT_REQUEST_METHODS",
    "classify_message",
    "notification_message",
    "request_message",
    "response_result",
]
