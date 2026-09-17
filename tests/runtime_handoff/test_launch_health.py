"""Launch readiness polling and failed-session stop semantics.

The v5 failure: a healthy service was reported unhealthy because ownership was
anchored on a launcher's transient pre-exec image, so the first identity
comparison failed and the health loop returned without ever polling.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from code_mule.domain import (
    PlanStatus,
    ProjectRevision,
    ProjectStatus,
    RevisionCheckStatus,
    RevisionStatus,
    TaskStatus,
)
from code_mule.runtime_handoff import (
    LaunchDisposition,
    RuntimeHealthStatus,
    RuntimeLaunchBlocked,
    RuntimeOwnershipUncertain,
    RuntimeSession,
    RuntimeSessionStatus,
    parse_manifest_candidate,
)
from code_mule.runtime_handoff.service import (
    ProcessObservation,
    RuntimeHandoffService,
)
from code_mule.state.store import JsonProjectStateStore
from state import make_project_state

from .test_contracts import candidate


NOW = datetime(2026, 9, 18, tzinfo=UTC)
START_A = "a" * 64
COMMAND_A = "b" * 64
START_B = "c" * 64
COMMAND_B = "d" * 64


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git",) + args, cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class Script:
    """Call-indexed values that repeat their final entry forever."""

    def __init__(self, values: list) -> None:
        self.values = values
        self.calls = 0

    def next(self):
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


class FakeProcess:
    def __init__(self, pid: int = 80001) -> None:
        self.pid = pid
        self.terminated = False
        self.exit_code: int | None = None

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        return self.exit_code if self.exit_code is not None else 0

    def terminate(self):
        self.terminated = True
        self.exit_code = -15

    def kill(self):
        self.exit_code = -9


DECISION = ProcessObservation(True, START_B, COMMAND_B)


def ready_store(root: Path, *, startup_timeout: float = 1.0):
    git(root, "init")
    git(root, "config", "user.name", "Code Mule Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / ".git/info/exclude").write_text(".code-mule/\n", encoding="utf-8")
    (root / "index.html").write_text("runtime handoff", encoding="utf-8")
    payload = candidate()
    payload["launch_spec"]["startup_timeout_seconds"] = startup_timeout
    (root / "code-mule-delivery.json").write_text(json.dumps(payload), encoding="utf-8")
    git(root, "add", "--", ".")
    git(root, "commit", "-q", "-m", "deliver project")
    head = git(root, "rev-parse", "HEAD")
    manifest = parse_manifest_candidate(
        payload,
        project_id="project-1",
        revision_number=1,
        plan_version=1,
        generated_at=NOW,
    )
    base = make_project_state()
    state = replace(
        base,
        project=replace(
            base.project,
            status=ProjectStatus.DONE,
            current_task_id=None,
            workspace=str(root),
        ),
        plans=(replace(base.plans[0], status=PlanStatus.COMPLETED),),
        tasks=(replace(base.tasks[0], status=TaskStatus.COMPLETED),),
        revisions=(
            ProjectRevision(
                1, NOW, RevisionStatus.COMPLETED, "plan-1", 1,
                completed_at=NOW, completion_head=head,
                verification_status=RevisionCheckStatus.PASS,
                final_review_status=RevisionCheckStatus.PASS,
                verification_result_id="vr-1",
            ),
        ),
        delivery_manifest_required=True,
        delivery_manifests=(manifest,),
    )
    state_path = root / ".code-mule" / "project-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    store = JsonProjectStateStore(state_path)
    store.save(state)
    return store, manifest


def build_service(
    store,
    *,
    identities: Script | None = None,
    observe=None,
    http: Script,
    process: FakeProcess | None = None,
    port: int = 49441,
    signals: list | None = None,
    sleeper=lambda seconds: None,
):
    process = process or FakeProcess()
    signals = [] if signals is None else signals

    observer = observe
    if observer is None:
        def observer(pid):
            return identities.next()

    return (
        RuntimeHandoffService(
            store=store,
            clock=lambda: NOW,
            session_id_factory=lambda: "runtime-1",
            event_id_factory=lambda: "event-1",
            environment={"PATH": "/usr/bin:/bin"},
            popen=lambda argv, **kwargs: process,
            process_observer=observer,
            http_status=lambda url, timeout: http.next(),
            dynamic_port_factory=lambda: port,
            port_available=lambda value: True,
            process_signaler=lambda pid, number: signals.append((pid, number)),
            sleeper=sleeper,
        ),
        process,
        signals,
    )


class LaunchReadinessTests(unittest.TestCase):
    def test_first_connection_refused_then_success_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _manifest = ready_store(root)
            service, _process, _signals = build_service(
                store,
                identities=Script([DECISION]),
                http=Script([None, None, 200]),
            )
            outcome = service.launch()
            self.assertEqual(outcome.disposition, LaunchDisposition.STARTED)
            self.assertEqual(outcome.session.status.value, "running")
            self.assertEqual(outcome.session.health_status.value, "healthy")
            event = next(
                item for item in store.load().events
                if item.event_type == "runtime.health_checked"
            )
            self.assertEqual(event.metadata["health_attempts"], "3")
            self.assertEqual(event.metadata["health_failure"], "none")
            self.assertEqual(event.metadata["health_http_status"], "200")

    def test_transient_launcher_identity_change_is_not_terminal(self):
        """The exact v5 shape: a re-exec between startup and the health loop."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _manifest = ready_store(root)
            service, _process, _signals = build_service(
                store,
                # transient launcher image, then the settled interpreter image
                identities=Script([
                    ProcessObservation(True, START_A, COMMAND_A),
                    ProcessObservation(True, START_B, COMMAND_B),
                    DECISION,
                ]),
                http=Script([None, 200]),
            )
            outcome = service.launch()
            self.assertEqual(outcome.session.status.value, "running")
            self.assertEqual(outcome.session.health_status.value, "healthy")
            self.assertEqual(
                outcome.session.command_fingerprint, COMMAND_B,
                "ownership must anchor on the settled image",
            )

    def test_persistent_identity_mismatch_never_signals_an_unowned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _manifest = ready_store(root, startup_timeout=0.3)
            service, _process, signals = build_service(
                store,
                identities=Script([ProcessObservation(True, START_B, COMMAND_B)]),
                http=Script([200]),
            )
            state = store.load()
            session = replace(
                RuntimeSession(
                    id="runtime-1", project_id=state.project.id,
                    revision_number=1, manifest_id="manifest-r1-1",
                    pid=80001, started_at=NOW,
                    status=RuntimeSessionStatus.FAILED,
                    access_url="http://127.0.0.1:49441/",
                    health_status=RuntimeHealthStatus.UNHEALTHY,
                    process_start_identity=START_A,
                    command_fingerprint=COMMAND_A,
                )
            )
            store.save(replace(state, runtime_sessions=(session,)))
            with self.assertRaises(RuntimeOwnershipUncertain):
                service.stop_app()
            self.assertEqual(signals, [], "an unowned process must never be signalled")

    def test_deadline_expiry_is_a_health_failure_with_bounded_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _manifest = ready_store(root, startup_timeout=0.3)
            service, _process, signals = build_service(
                store,
                identities=Script([DECISION]),
                http=Script([None]),
            )
            with self.assertRaises(RuntimeLaunchBlocked) as caught:
                service.launch()
            self.assertIn("health check failed", str(caught.exception))
            session = store.load().runtime_sessions[-1]
            self.assertEqual(session.status.value, "failed")
            event = next(
                item for item in store.load().events
                if item.event_type == "runtime.health_checked"
            )
            self.assertEqual(event.metadata["health_failure"], "connection_failed")
            self.assertGreater(int(event.metadata["health_attempts"]), 0)
            self.assertEqual(event.metadata["health_status"], "unhealthy")
            # Owned process is cleaned up, never orphaned.
            self.assertEqual([pid for pid, _ in signals], [80001])

    def test_process_exit_before_healthy_is_a_typed_process_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _manifest = ready_store(root)
            service, _process, _signals = build_service(
                store,
                identities=Script([
                    DECISION,
                    DECISION,
                    ProcessObservation(False, None, None),
                ]),
                http=Script([None]),
            )
            with self.assertRaises(RuntimeLaunchBlocked) as caught:
                service.launch()
            self.assertIn("exited before becoming healthy", str(caught.exception))
            event = next(
                item for item in store.load().events
                if item.event_type == "runtime.health_checked"
            )
            self.assertEqual(event.metadata["health_failure"], "process_exited")

    def test_dynamic_port_is_substituted_into_the_health_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _manifest = ready_store(root)
            seen: list[str] = []
            service, _process, _signals = build_service(
                store,
                identities=Script([DECISION]),
                http=Script([200]),
                port=45678,
            )
            service._http_status = lambda url, timeout: seen.append(url) or 200
            service.launch()
            self.assertEqual(seen, ["http://127.0.0.1:45678/"])

    def test_expected_status_200_marks_the_session_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _manifest = ready_store(root)
            service, _process, _signals = build_service(
                store, identities=Script([DECISION]), http=Script([200])
            )
            self.assertEqual(service.launch().session.health_status.value, "healthy")


class FailedSessionStopTests(unittest.TestCase):
    def _failed_session_state(self, root: Path, **overrides):
        store, _manifest = ready_store(root)
        state = store.load()
        session = RuntimeSession(
            id="runtime-1",
            project_id=state.project.id,
            revision_number=1,
            manifest_id="manifest-r1-1",
            pid=91001,
            started_at=NOW,
            status=RuntimeSessionStatus.FAILED,
            access_url="http://127.0.0.1:49441/",
            health_status=RuntimeHealthStatus.UNHEALTHY,
            process_start_identity=START_A,
            command_fingerprint=COMMAND_A,
        )
        store.save(replace(state, runtime_sessions=(replace(session, **overrides),)))
        return store, session

    def test_failed_session_with_owned_live_process_can_still_be_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _session = self._failed_session_state(root)
            signals: list = []

            def observe(pid):
                # Alive until Code Mule signals it, then gone.
                return ProcessObservation(not signals, START_A, COMMAND_A)

            service, _process, _ = build_service(
                store,
                observe=observe,
                http=Script([200]),
                signals=signals,
            )
            stopped = service.stop_app()
            self.assertEqual(signals, [(91001, 15)])
            self.assertEqual(stopped.status.value, "stopped")
            self.assertEqual(stopped.stop_requested_at, NOW)

    def test_failed_session_with_mismatched_pid_is_never_touched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _session = self._failed_session_state(root)
            signals: list = []
            service, _process, _ = build_service(
                store,
                identities=Script([ProcessObservation(True, START_B, COMMAND_B)]),
                http=Script([200]),
                signals=signals,
            )
            with self.assertRaises(RuntimeOwnershipUncertain):
                service.stop_app()
            self.assertEqual(signals, [])
            self.assertEqual(
                store.load().runtime_sessions[-1].status.value, "ownership_uncertain"
            )

    def test_failed_session_whose_process_is_gone_becomes_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _session = self._failed_session_state(root)
            signals: list = []
            service, _process, signals = build_service(
                store,
                identities=Script([ProcessObservation(False, None, None)]),
                http=Script([200]),
                signals=signals,
            )
            exited = service.stop_app()
            self.assertEqual(exited.status.value, "exited")
            self.assertEqual(signals, [])

    def test_cross_terminal_session_recovery_reports_the_stopped_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _session = self._failed_session_state(root)
            signals: list = []

            def observe(pid):
                return ProcessObservation(not signals, START_A, COMMAND_A)

            first, _process, _ = build_service(
                store,
                observe=observe,
                http=Script([200]),
                signals=signals,
            )
            first.stop_app()
            # A second composition over the same store, as a new Terminal does.
            second, _process, _ = build_service(
                store,
                identities=Script([ProcessObservation(False, None, None)]),
                http=Script([200]),
            )
            recovered = second.app_status()
            self.assertEqual(recovered.status.value, "stopped")
            self.assertEqual(recovered.id, "runtime-1")


if __name__ == "__main__":
    unittest.main()
