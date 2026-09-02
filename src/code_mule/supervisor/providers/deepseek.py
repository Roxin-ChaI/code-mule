"""DeepSeek Responses API adapter for Supervisor structured reasoning."""

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Protocol, cast

from code_mule.supervisor.contracts import SupervisorOperation


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
    ) -> None:
        super().__init__(message)
        self.status = status
        self.incomplete_reason = incomplete_reason
        self.error_code = error_code
        self.error_message = error_message


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

        response = self._client.responses.create(**request)
        status = _string_field(response, "status")
        if status == "incomplete":
            incomplete_details = getattr(response, "incomplete_details", None)
            incomplete_reason = _string_field(incomplete_details, "reason")
            raise DeepSeekSupervisorResponseError(
                f"DeepSeek response incomplete: reason={incomplete_reason!r}",
                status=status,
                incomplete_reason=incomplete_reason,
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
            )
        if status != "completed":
            raise DeepSeekSupervisorResponseError(
                f"DeepSeek response status is not completed: {status!r}",
                status=status,
            )

        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or output_text == "":
            raise DeepSeekSupervisorResponseError(
                "DeepSeek completed response has no output_text",
                status=status,
            )
        try:
            payload: object = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise DeepSeekSupervisorResponseError(
                "DeepSeek output_text is not valid JSON",
                status=status,
            ) from error
        if not isinstance(payload, dict):
            raise DeepSeekSupervisorResponseError(
                "DeepSeek output_text must contain a JSON object",
                status=status,
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


__all__ = [
    "DeepSeekSupervisorConfig",
    "DeepSeekSupervisorModelClient",
    "DeepSeekSupervisorResponseError",
]
