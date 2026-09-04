"""Application service for one isolated Codex Worker execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from code_mule.domain.models import ExecutionReport
from code_mule.progress import ProgressSink

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


class CodexWorkerSession:
    """Reuse one initialized Codex thread across multiple task attempts."""

    def __init__(
        self,
        config: CodexWorkerConfig,
        *,
        client_factory: _ClientFactory | None = None,
        progress_sink: ProgressSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client_factory is None:
            self._client = CodexAppServerClient(
                config,
                progress_sink=progress_sink,
                clock=clock,
            )
        else:
            self._client = client_factory(config)
        self._thread_id: str | None = None
        self._closed = False

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def closed(self) -> bool:
        """Whether the local app-server boundary has been closed."""

        return self._closed

    def start(self) -> None:
        if self._thread_id is not None:
            return
        self._client.initialize()
        self._thread_id = self._client.start_thread()

    def execute(
        self,
        request: WorkerTaskRequest,
        *,
        report_id: str,
        created_at: datetime,
    ) -> ExecutionReport:
        if self._thread_id is None:
            raise RuntimeError("Codex Worker session has not been started")
        turn_id = self._client.start_turn(
            self._thread_id,
            request.prompt,
            output_schema=structured_worker_report_schema(),
        )
        result = self._client.wait_for_turn(self._thread_id, turn_id)
        report = parse_structured_worker_report(result.final_message)
        return build_execution_report(
            request=request,
            result=report,
            report_id=report_id,
            created_at=created_at,
            transport_issues=result.issues,
        )

    def close(self) -> None:
        self._client.close()
        self._closed = True

    def __enter__(self) -> CodexWorkerSession:
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class CodexWorkerService:
    """Execute one task through a fresh local app-server process."""

    def __init__(
        self,
        config: CodexWorkerConfig,
        *,
        client_factory: _ClientFactory | None = None,
        progress_sink: ProgressSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory
        self._progress_sink = progress_sink
        self._clock = clock

    def execute(
        self,
        request: WorkerTaskRequest,
        *,
        report_id: str,
        created_at: datetime,
    ) -> ExecutionReport:
        with CodexWorkerSession(
            self._config,
            client_factory=self._client_factory,
            progress_sink=self._progress_sink,
            clock=self._clock,
        ) as session:
            return session.execute(
                request,
                report_id=report_id,
                created_at=created_at,
            )


__all__ = ["CodexWorkerService", "CodexWorkerSession"]
