"""DeepSeek Responses API adapter for Supervisor structured reasoning."""

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Protocol, cast

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)

from code_mule.supervisor.contracts import (
    SupervisorFailureCategory,
    SupervisorOperation,
)


class DeepSeekSupervisorResponseError(RuntimeError):
    """Raised when a DeepSeek response cannot yield a valid JSON object."""

    def __init__(
        self,
        message: str,
        *,
        status: str | None = None,
        incomplete_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        failure_category: SupervisorFailureCategory,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.incomplete_reason = incomplete_reason
        self.error_code = error_code
        self.error_message = error_message
        self.failure_category = failure_category


@dataclass(frozen=True)
class DeepSeekSupervisorConfig:
    model: str
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.model == "":
            raise ValueError("model must not be empty")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")


class _ResponsesAPI(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _DeepSeekCompatibleClient(Protocol):
    responses: _ResponsesAPI


_SCHEMA_NAMES = {
    SupervisorOperation.PLAN: "code_mule_plan",
    SupervisorOperation.REVIEW: "code_mule_review",
    SupervisorOperation.IMPACT_ANALYSIS: "code_mule_impact_analysis",
    SupervisorOperation.PROGRESS_REPORT: "code_mule_progress_report",
    SupervisorOperation.BOSS_ROUTING: "code_mule_boss_routing",
}


class DeepSeekSupervisorModelClient:
    """Translate Supervisor requests to one stateless DeepSeek Responses call."""

    def __init__(
        self,
        client: _DeepSeekCompatibleClient,
        config: DeepSeekSupervisorConfig,
    ):
        self._client = client
        self._config = config

    def create_structured_response(
        self,
        *,
        operation: SupervisorOperation,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "model": self._config.model,
            "instructions": system_prompt,
            "input": user_prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": _SCHEMA_NAMES[operation],
                    "schema": deepcopy(schema),
                }
            },
        }
        if self._config.max_output_tokens is not None:
            request["max_output_tokens"] = self._config.max_output_tokens

        try:
            response = self._client.responses.create(**request)
        except (APITimeoutError, TimeoutError) as error:
            raise DeepSeekSupervisorResponseError(
                "DeepSeek request timed out",
                failure_category=SupervisorFailureCategory.TRANSPORT_TIMEOUT,
            ) from error
        except (AuthenticationError, PermissionDeniedError) as error:
            raise DeepSeekSupervisorResponseError(
                "DeepSeek provider authentication failed",
                failure_category=(
                    SupervisorFailureCategory.PROVIDER_AUTHENTICATION
                ),
            ) from error
        except (BadRequestError, NotFoundError, UnprocessableEntityError) as error:
            raise DeepSeekSupervisorResponseError(
                "DeepSeek provider configuration was rejected",
                failure_category=(
                    SupervisorFailureCategory.PROVIDER_CONFIGURATION
                ),
            ) from error
        except (APIConnectionError, ConnectionError) as error:
            raise DeepSeekSupervisorResponseError(
                "DeepSeek temporary connection failure",
                failure_category=(
                    SupervisorFailureCategory.TEMPORARY_CONNECTION_FAILURE
                ),
            ) from error
        status = _string_field(response, "status")
        if status == "incomplete":
            incomplete_details = getattr(response, "incomplete_details", None)
            incomplete_reason = _string_field(incomplete_details, "reason")
            raise DeepSeekSupervisorResponseError(
                f"DeepSeek response incomplete: reason={incomplete_reason!r}",
                status=status,
                incomplete_reason=incomplete_reason,
                failure_category=(
                    SupervisorFailureCategory.INCOMPLETE_MAX_OUTPUT_TOKENS
                    if incomplete_reason == "max_output_tokens"
                    else (
                        SupervisorFailureCategory.CONTENT_FILTER
                        if incomplete_reason == "content_filter"
                        else SupervisorFailureCategory.UNKNOWN_FAILURE
                    )
                ),
            )
        if status == "failed":
            response_error = getattr(response, "error", None)
            error_code = _string_field(response_error, "code")
            error_message = _string_field(response_error, "message")
            raise DeepSeekSupervisorResponseError(
                "DeepSeek response failed: "
                f"code={error_code!r}, message={error_message!r}",
                status=status,
                error_code=error_code,
                error_message=error_message,
                failure_category=_failed_response_category(error_code),
            )
        if status != "completed":
            raise DeepSeekSupervisorResponseError(
                f"DeepSeek response status is not completed: {status!r}",
                status=status,
                failure_category=SupervisorFailureCategory.UNKNOWN_FAILURE,
            )

        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or output_text == "":
            raise DeepSeekSupervisorResponseError(
                "DeepSeek completed response has no output_text",
                status=status,
                failure_category=(
                    SupervisorFailureCategory.MALFORMED_STRUCTURED_RESPONSE
                ),
            )
        try:
            payload: object = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise DeepSeekSupervisorResponseError(
                "DeepSeek output_text is not valid JSON",
                status=status,
                failure_category=(
                    SupervisorFailureCategory.MALFORMED_STRUCTURED_RESPONSE
                ),
            ) from error
        if not isinstance(payload, dict):
            raise DeepSeekSupervisorResponseError(
                "DeepSeek output_text must contain a JSON object",
                status=status,
                failure_category=(
                    SupervisorFailureCategory.MALFORMED_STRUCTURED_RESPONSE
                ),
            )
        return cast(dict[str, object], payload)


def _string_field(value: object, field_name: str) -> str | None:
    if isinstance(value, dict):
        field = value.get(field_name)
    else:
        field = getattr(value, field_name, None)
    if not isinstance(field, str):
        return None
    return field[:500]


def _failed_response_category(
    error_code: str | None,
) -> SupervisorFailureCategory:
    if error_code in {
        "authentication_error",
        "permission_denied",
        "unauthorized",
    }:
        return SupervisorFailureCategory.PROVIDER_AUTHENTICATION
    if error_code in {
        "bad_request",
        "invalid_request_error",
        "model_not_found",
        "unsupported_parameter",
    }:
        return SupervisorFailureCategory.PROVIDER_CONFIGURATION
    if error_code in {
        "rate_limit_exceeded",
        "server_error",
        "service_unavailable",
    }:
        return SupervisorFailureCategory.TEMPORARY_CONNECTION_FAILURE
    if error_code == "content_filter":
        return SupervisorFailureCategory.CONTENT_FILTER
    return SupervisorFailureCategory.UNKNOWN_FAILURE


__all__ = [
    "DeepSeekSupervisorConfig",
    "DeepSeekSupervisorModelClient",
    "DeepSeekSupervisorResponseError",
]
