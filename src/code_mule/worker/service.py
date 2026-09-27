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
from .report_contract import REPORT_ENVELOPE_INSTRUCTION
from .structured_report import (
    InvalidWorkerReport,
    parse_structured_worker_report,
    structured_worker_report_schema,
)
from code_mule.transport import TransportDiagnostics, TransportState


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

    def transport_diagnostics(self) -> TransportDiagnostics: ...


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
        self._turn_id: str | None = None
        self._closed = False
        self._diagnostics: TransportDiagnostics | None = None

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def closed(self) -> bool:
        """Whether the local app-server boundary has been closed."""

        return self._closed

    @property
    def turn_id(self) -> str | None:
        """The current Codex turn identity, persisted with the attempt."""

        return self._turn_id

    def transport_diagnostics(self) -> TransportDiagnostics | None:
        """Bounded supervision facts captured for the last Worker boundary."""

        snapshot = getattr(self._client, "transport_diagnostics", None)
        if callable(snapshot):
            self._diagnostics = snapshot()
        return self._diagnostics

    @property
    def retryable_error_count(self) -> int:
        return getattr(self._client, "retryable_error_count", 0)

    @property
    def mcp_startup_error_count(self) -> int:
        return getattr(self._client, "mcp_startup_error_count", 0)

    @property
    def last_retryable_error_code(self) -> str | None:
        return getattr(self._client, "last_retryable_error_code", None)

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
        # The envelope is stated on the wire for every Worker, so the prompt
        # contract and the extractor can never drift apart.
        prompt = f"{request.prompt}\n\n{REPORT_ENVELOPE_INSTRUCTION}"
        turn_id = self._client.start_turn(
            self._thread_id,
            prompt,
            output_schema=structured_worker_report_schema(),
        )
        self._turn_id = turn_id
        result = self._client.wait_for_turn(self._thread_id, turn_id)
        # Capture trusted terminal evidence before any report parsing so a
        # parser failure can never masquerade as a missing terminal result.
        terminal = getattr(self._client, "terminal_evidence", None)
        self.mark_report_state(TransportState.REPORT_EXTRACTION)
        try:
            report = parse_structured_worker_report(result.final_message)
        except InvalidWorkerReport as error:
            # Keep the typed stage/code/field path intact; only attach the
            # terminal evidence observed before parsing.
            error.terminal = terminal
            raise
        self.mark_report_state(TransportState.REPORT_PARSED)
        execution_report = build_execution_report(
            request=request,
            result=report,
            report_id=report_id,
            created_at=created_at,
            transport_issues=result.issues,
        )
        self.mark_report_state(TransportState.REPORT_VALIDATED)
        return execution_report

    def mark_report_state(self, state: TransportState) -> None:
        """Advance the bounded report lifecycle without requiring a real client."""

        advance = getattr(self._client, "mark_report_state", None)
        if callable(advance):
            advance(state)

    def close(self) -> None:
        if self._diagnostics is None:
            self.transport_diagnostics()
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
