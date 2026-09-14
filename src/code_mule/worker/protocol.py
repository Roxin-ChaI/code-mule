"""Pure helpers for the Codex app-server JSONL protocol."""

from enum import StrEnum
import re
from typing import cast

from code_mule.domain.enums import CapabilityApprovalScope

from .contracts import (
    CapabilityApprovalAction,
    CapabilityApprovalDecision,
    CodexProtocolError,
    WorkerCapabilityApprovalRequest,
    WorkerInputRequest,
)


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
_SENSITIVE_APPROVAL_VALUE = re.compile(
    r"(?i)(?:(?:api[_ -]?key|token|password|authorization|credential)\s*[:=]\s*|bearer\s+|sk-)[^\s,;]+"
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


def server_response_message(
    request_id: int | str, decision: CapabilityApprovalDecision
) -> dict[str, object]:
    """Build the native response for one still-live MCP elicitation request."""

    if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
        raise ValueError("request_id must be a string or integer")
    content: dict[str, object] | None
    if decision.action is CapabilityApprovalAction.ACCEPT:
        content = {}
        if decision.scope not in (None, CapabilityApprovalScope.ONCE):
            content["persist"] = decision.scope.value
    else:
        content = None
    return {
        "id": request_id,
        "result": {"action": decision.action.value, "content": content},
    }


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
    if method == "mcpServer/elicitation/request" and _approval_meta(typed_params) is not None:
        raise CodexProtocolError("capability approval request is not Worker input")
    if method == "item/tool/requestUserInput":
        question, choices = _tool_input(typed_params)
    else:
        question, choices = _mcp_elicitation(typed_params)
    try:
        return WorkerInputRequest(method, request_id, question, choices)
    except ValueError as error:
        raise CodexProtocolError("user-input request exceeds safe bounds") from error


def is_capability_approval_request(message: object) -> bool:
    """Recognize only the documented structured MCP approval marker."""

    if classify_message(message) is not MessageKind.SERVER_REQUEST:
        return False
    payload = cast(dict[str, object], message)
    if payload.get("method") != "mcpServer/elicitation/request":
        return False
    params = payload.get("params")
    return (
        isinstance(params, dict)
        and all(isinstance(key, str) for key in params)
        and _approval_meta(cast(dict[str, object], params)) is not None
    )


def parse_capability_approval_request(
    message: object,
) -> WorkerCapabilityApprovalRequest:
    """Project a native capability request without retaining its raw payload."""

    if classify_message(message) is not MessageKind.SERVER_REQUEST:
        raise CodexProtocolError("expected an app-server server request")
    payload = cast(dict[str, object], message)
    if payload.get("method") != "mcpServer/elicitation/request":
        raise CodexProtocolError("server request is not an MCP elicitation")
    protocol_request_id = payload.get("id")
    _safe_request_id(protocol_request_id)
    params = payload.get("params")
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        raise CodexProtocolError("elicitation params must be an object")
    typed_params = cast(dict[str, object], params)
    meta = _approval_meta(typed_params)
    if meta is None:
        raise CodexProtocolError("elicitation is not a structured capability approval")
    thread_id = _bounded_field(typed_params, "threadId", 128, required=True)
    turn_id = _bounded_field(typed_params, "turnId", 128, required=True)
    server_name = _bounded_field(typed_params, "serverName", 200, required=True)
    request, _ = _mcp_elicitation(typed_params)
    capability_id = _bounded_field(meta, "connector_id", 200)
    connector_name = _bounded_field(meta, "connector_name", 200)
    tool_name = _bounded_field(meta, "tool_name", 200)
    tool_title = _bounded_field(meta, "tool_title", 200)
    capability = (
        "Computer Use"
        if capability_id == "browser-use"
        else connector_name or tool_title or tool_name or server_name
    )
    application = connector_name
    scopes = _approval_scopes(meta.get("persist"))
    try:
        return WorkerCapabilityApprovalRequest(
            method="mcpServer/elicitation/request",
            protocol_request_id=cast(int | str, protocol_request_id),
            thread_id=cast(str, thread_id),
            turn_id=cast(str, turn_id),
            server_name=cast(str, server_name),
            request=request,
            capability=capability,
            application=application,
            capability_id=capability_id,
            tool_name=tool_name,
            available_scopes=scopes,
        )
    except ValueError as error:
        raise CodexProtocolError("capability approval exceeds safe bounds") from error


def parse_native_approval_request(
    message: object,
) -> WorkerCapabilityApprovalRequest:
    """Project a native Codex sandbox/tool approval into bounded typed fields.

    These JSON-RPC server requests are connection-bound.  Code Mule retains
    only the request identity and a small structured operation fingerprint; it
    never persists the raw request payload or free-form command output.
    """

    if classify_message(message) is not MessageKind.SERVER_REQUEST:
        raise CodexProtocolError("expected an app-server server request")
    payload = cast(dict[str, object], message)
    method = cast(str, payload.get("method"))
    if method not in APPROVAL_REQUEST_METHODS:
        raise CodexProtocolError("server request is not a native approval method")
    protocol_request_id = payload.get("id")
    _safe_request_id(protocol_request_id)
    params = payload.get("params")
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        raise CodexProtocolError(f"{method} params must be an object")
    typed_params = cast(dict[str, object], params)
    thread_id = _bounded_field(typed_params, "threadId", 128, required=True)
    turn_id = _bounded_field(typed_params, "turnId", 128, required=True)

    command = _safe_command_identity(typed_params.get("command"))
    purpose = _optional_safe_text(typed_params.get("reason"), 200)
    target = _optional_safe_text(
        typed_params.get("cwd", typed_params.get("sandboxPolicy")), 200
    )
    sandbox = method in {
        "item/commandExecution/requestApproval",
        "item/permissions/requestApproval",
        "execCommandApproval",
    }
    capability = "Sandbox escalation" if sandbox else "File change approval"
    try:
        return WorkerCapabilityApprovalRequest(
            method=method,
            protocol_request_id=cast(int | str, protocol_request_id),
            thread_id=cast(str, thread_id),
            turn_id=cast(str, turn_id),
            server_name="codex-app-server",
            request="Approve the exact native Codex operation",
            capability=capability,
            application=purpose,
            capability_id=target,
            tool_name=command,
            available_scopes=(CapabilityApprovalScope.ONCE,),
        )
    except ValueError as error:
        raise CodexProtocolError("native approval exceeds safe bounds") from error


def _optional_safe_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "" or len(value) > limit:
        raise CodexProtocolError("native approval field exceeds safe bounds")
    return _sanitize_approval_text(value)


def _safe_command_identity(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        command = value
    elif isinstance(value, list) and value and all(
        isinstance(item, str) and item for item in value
    ):
        command = " ".join(cast(list[str], value))
    else:
        raise CodexProtocolError("native approval command identity is invalid")
    if len(command) > 200:
        raise CodexProtocolError("native approval command identity exceeds safe bounds")
    return _sanitize_approval_text(command)


def _sanitize_approval_text(value: str) -> str:
    return _SENSITIVE_APPROVAL_VALUE.sub("[REDACTED]", _sanitize_text(value))


def _approval_meta(params: dict[str, object]) -> dict[str, object] | None:
    value = params.get("_meta")
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CodexProtocolError("elicitation _meta must be an object")
    meta = cast(dict[str, object], value)
    marker = meta.get("codex_approval_kind")
    if marker is None:
        return None
    if marker != "mcp_tool_call":
        raise CodexProtocolError("elicitation has an unknown approval kind")
    return meta


def _bounded_field(
    values: dict[str, object], name: str, limit: int, *, required: bool = False
) -> str | None:
    value = values.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or value == "" or len(value) > limit:
        raise CodexProtocolError(f"capability approval {name} is invalid")
    return _sanitize_text(value)


def _approval_scopes(value: object) -> tuple[CapabilityApprovalScope, ...]:
    offered: list[CapabilityApprovalScope] = [CapabilityApprovalScope.ONCE]
    raw = [] if value is None else value if isinstance(value, list) else [value]
    if not all(item in {"session", "always"} for item in raw):
        raise CodexProtocolError("capability approval persistence scope is invalid")
    for item in raw:
        scope = CapabilityApprovalScope(cast(str, item))
        if scope not in offered:
            offered.append(scope)
    return tuple(offered)


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
    "is_capability_approval_request",
    "notification_message",
    "parse_capability_approval_request",
    "parse_native_approval_request",
    "parse_worker_input_request",
    "request_message",
    "response_result",
    "server_response_message",
]
