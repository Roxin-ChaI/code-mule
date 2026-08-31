"""Presentation-independent progress sinks."""

from collections.abc import Callable, Iterable
from threading import Lock
from typing import Protocol

from .contracts import ProgressEvent


class ProgressSink(Protocol):
    def emit(self, event: ProgressEvent) -> None: ...


class NoopProgressSink:
    def emit(self, event: ProgressEvent) -> None:
        return None


class RecordingProgressSink:
    """Thread-safe in-memory sink for tests and composition diagnostics."""

    def __init__(self) -> None:
        self._events: list[ProgressEvent] = []
        self._lock = Lock()

    @property
    def events(self) -> tuple[ProgressEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def emit(self, event: ProgressEvent) -> None:
        with self._lock:
            self._events.append(event)


class CompositeProgressSink:
    """Fan out in order while isolating optional presentation failures."""

    def __init__(
        self,
        sinks: Iterable[ProgressSink],
        *,
        error_handler: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._sinks = tuple(sinks)
        self._error_handler = error_handler
        self._errors: list[BaseException] = []
        self._lock = Lock()

    @property
    def errors(self) -> tuple[BaseException, ...]:
        with self._lock:
            return tuple(self._errors)

    def emit(self, event: ProgressEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except BaseException as error:
                with self._lock:
                    self._errors.append(error)
                if self._error_handler is not None:
                    try:
                        self._error_handler(error)
                    except BaseException as handler_error:
                        with self._lock:
                            self._errors.append(handler_error)


def resilient_progress_sink(sink: ProgressSink | None) -> CompositeProgressSink:
    """Wrap one optional sink so telemetry cannot stop core execution."""

    return CompositeProgressSink((sink or NoopProgressSink(),))


__all__ = [
    "CompositeProgressSink",
    "NoopProgressSink",
    "ProgressSink",
    "RecordingProgressSink",
    "resilient_progress_sink",
]
