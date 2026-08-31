"""Application service for one isolated Codex Worker execution."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from code_mule.domain.models import ExecutionReport

from .client import CodexAppServerClient
from .contracts import CodexWorkerConfig, WorkerTaskRequest, WorkerTurnResult
from .parsing import build_execution_report


class _WorkerClient(Protocol):
    def initialize(self) -> None: ...

    def start_thread(self) -> str: ...

    def start_turn(self, thread_id: str, prompt: str) -> str: ...

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
            turn_id = client.start_turn(thread_id, request.prompt)
            result = client.wait_for_turn(thread_id, turn_id)
            return build_execution_report(
                request=request,
                result=result,
                report_id=report_id,
                created_at=created_at,
            )
        finally:
            client.close()


__all__ = ["CodexWorkerService"]
