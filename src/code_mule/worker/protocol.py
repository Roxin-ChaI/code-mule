"""Pure helpers for the Codex app-server JSONL protocol."""

from enum import StrEnum
from typing import cast

from .contracts import CodexProtocolError, WorkerInputRequest


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


def parse_worker_input_request(message: object) -> WorkerInputRequest:
    """Project one supported server request into bounded, non-secret fields."""

    if classify_message(message) is not MessageKind.SERVER_REQUEST:
        raise CodexProtocolError("expected an app-server server request")
    payload = cast(dict[str, object], message)
    method = cast(str, payload["method"])
    if method not in USER_INPUT_REQUEST_METHODS:
        raise CodexProtocolError("server request is not a supported user-input method")
    request_id = _safe_request_id(payload.get("id"))
    params = payload.get("params")
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        raise CodexProtocolError(f"{method} params must be an object")
    typed_params = cast(dict[str, object], params)
    if method == "item/tool/requestUserInput":
        question, choices = _tool_input(typed_params)
    else:
        question, choices = _mcp_elicitation(typed_params)
    try:
        return WorkerInputRequest(method, request_id, question, choices)
    except ValueError as error:
        raise CodexProtocolError("user-input request exceeds safe bounds") from error


def _safe_request_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CodexProtocolError("user-input request id must be a string or integer")
    request_id = str(value)
    if request_id == "" or len(request_id) > 128:
        raise CodexProtocolError("user-input request id exceeds safe bounds")
    return request_id


def _tool_input(params: dict[str, object]) -> tuple[str, tuple[str, ...]]:
    questions = params.get("questions")
    if not isinstance(questions, list) or not questions or len(questions) > 10:
        raise CodexProtocolError("requestUserInput requires bounded questions")
    prompts: list[str] = []
    choices: list[str] = []
    for item in questions:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise CodexProtocolError("requestUserInput question must be an object")
        question = item.get("question")
        if not isinstance(question, str) or question == "":
            raise CodexProtocolError("requestUserInput question must be non-empty")
        prompts.append(_sanitize_text(question))
        options = item.get("options", [])
        if not isinstance(options, list):
            raise CodexProtocolError("requestUserInput options must be a list")
        for option in options:
            if not isinstance(option, dict) or not all(
                isinstance(key, str) for key in option
            ):
                raise CodexProtocolError("requestUserInput option must be an object")
            label = option.get("label")
            if not isinstance(label, str) or label == "":
                raise CodexProtocolError("requestUserInput option label must be non-empty")
            choices.append(_sanitize_text(label))
    return "\n".join(prompts), tuple(dict.fromkeys(choices))


def _mcp_elicitation(params: dict[str, object]) -> tuple[str, tuple[str, ...]]:
    message = params.get("message")
    if not isinstance(message, str) or message == "":
        raise CodexProtocolError("elicitation request requires a non-empty message")
    schema = params.get("requestedSchema")
    if schema is None:
        return _sanitize_text(message), ()
    if not isinstance(schema, dict) or not all(isinstance(key, str) for key in schema):
        raise CodexProtocolError("elicitation requestedSchema must be an object")
    choices: list[str] = []
    _collect_schema_choices(cast(dict[str, object], schema), choices, depth=0)
    return _sanitize_text(message), tuple(dict.fromkeys(choices))


def _collect_schema_choices(
    schema: dict[str, object], choices: list[str], *, depth: int
) -> None:
    if depth > 3:
        raise CodexProtocolError("elicitation schema exceeds safe nesting depth")
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not all(
            isinstance(item, str) and item != "" for item in enum
        ):
            raise CodexProtocolError("elicitation enum must contain strings")
        choices.extend(_sanitize_text(cast(str, item)) for item in enum)
    properties = schema.get("properties")
    if properties is None:
        return
    if not isinstance(properties, dict) or not all(
        isinstance(key, str) for key in properties
    ):
        raise CodexProtocolError("elicitation properties must be an object")
    if len(properties) > 20:
        raise CodexProtocolError("elicitation properties exceed safe bounds")
    for value in properties.values():
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise CodexProtocolError("elicitation property must be an object")
        _collect_schema_choices(
            cast(dict[str, object], value), choices, depth=depth + 1
        )


def _sanitize_text(value: str) -> str:
    sanitized = "".join(
        character
        for character in value
        if character in {"\n", "\t"} or ord(character) >= 32
    ).strip()
    if sanitized == "":
        raise CodexProtocolError("user-input text is empty after sanitization")
    return sanitized


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
    "parse_worker_input_request",
    "request_message",
    "response_result",
]
