"""Provider-neutral model boundary for Supervisor reasoning."""

from typing import Protocol

from .contracts import SupervisorOperation


class SupervisorModelClient(Protocol):
    def create_structured_response(
        self,
        *,
        operation: SupervisorOperation,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
    ) -> dict[str, object]: ...


__all__ = ["SupervisorModelClient"]
