"""Application service for one isolated Codex Worker execution."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from code_mule.domain.models import ExecutionReport

from .client import CodexAppServerClient
from .contracts import CodexWorkerConfig, WorkerTaskRequest, WorkerTurnResult
from .parsing import build_execution_report
from .structured_report import (
    parse_structured_worker_report,
    structured_worker_report_schema,
)


class _WorkerClient(Protocol):
    def initialize(self) -> None: ...

    def start_thread(self) -> str: ...

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        output_schema: dict[str, object] | None = None,
    ) -> str: ...

    def wait_for_turn(self, thread_id: str, turn_id: str) -> WorkerTurnResult: ...

    def close(self) -> None: ...


_ClientFactory = Callable[[CodexWorkerConfig], _WorkerClient]


class CodexWorkerService:
    """Execute one task through a fresh local app-server process."""

    def __init__(
        self,
        config: CodexWorkerConfig,
        *,
        client_factory: _ClientFactory = CodexAppServerClient,
    ) -> None:
        self._config = config
        self._client_factory = client_factory

    def execute(
        self,
        request: WorkerTaskRequest,
        *,
        report_id: str,
        created_at: datetime,
    ) -> ExecutionReport:
        client = self._client_factory(self._config)
        try:
            client.initialize()
            thread_id = client.start_thread()
            turn_id = client.start_turn(
                thread_id,
                request.prompt,
                output_schema=structured_worker_report_schema(),
            )
            result = client.wait_for_turn(thread_id, turn_id)
            report = parse_structured_worker_report(result.final_message)
            return build_execution_report(
                request=request,
                result=report,
                report_id=report_id,
                created_at=created_at,
                transport_issues=result.issues,
            )
        finally:
            client.close()


__all__ = ["CodexWorkerService"]
