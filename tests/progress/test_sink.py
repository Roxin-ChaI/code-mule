import unittest
from datetime import UTC, datetime

from code_mule.progress import (
    CompositeProgressSink,
    NoopProgressSink,
    ProgressEvent,
    ProgressEventType,
    RecordingProgressSink,
)


EVENT = ProgressEvent(
    ProgressEventType.WAITING,
    datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    "project-1",
    None,
    None,
    "Waiting",
    {},
)


class _OrderedSink:
    def __init__(self, name, calls, *, error=None):
        self.name = name
        self.calls = calls
        self.error = error

    def emit(self, event):
        self.calls.append((self.name, event))
        if self.error is not None:
            raise self.error


class ProgressSinkTests(unittest.TestCase):
    def test_noop_and_recording_sinks(self):
        NoopProgressSink().emit(EVENT)
        recording = RecordingProgressSink()
        recording.emit(EVENT)
        self.assertEqual(recording.events, (EVENT,))

    def test_composite_preserves_order_and_continues_after_sink_failure(self):
        calls = []
        observed_errors = []
        failure = RuntimeError("renderer failed")
        composite = CompositeProgressSink(
            (
                _OrderedSink("first", calls),
                _OrderedSink("broken", calls, error=failure),
                _OrderedSink("last", calls),
            ),
            error_handler=observed_errors.append,
        )
        composite.emit(EVENT)
        self.assertEqual([name for name, _ in calls], ["first", "broken", "last"])
        self.assertEqual(composite.errors, (failure,))
        self.assertEqual(observed_errors, [failure])

    def test_error_handler_failure_is_recorded_not_raised(self):
        sink_error = RuntimeError("sink")
        handler_error = RuntimeError("handler")

        def broken_handler(error):
            raise handler_error

        composite = CompositeProgressSink(
            (_OrderedSink("broken", [], error=sink_error),),
            error_handler=broken_handler,
        )
        composite.emit(EVENT)
        self.assertEqual(composite.errors, (sink_error, handler_error))


if __name__ == "__main__":
    unittest.main()
