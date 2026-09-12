from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from code_mule.domain import PlanStatus, ProjectRevision, ProjectStatus, RevisionCheckStatus, RevisionStatus, TaskStatus
from code_mule.runtime_handoff import (
    DeliveryManifestStatus,
    LaunchDisposition,
    RuntimeLaunchBlocked,
    RuntimeOwnershipUncertain,
    parse_manifest_candidate,
)
from code_mule.runtime_handoff.service import RuntimeHandoffService
from code_mule.runtime_handoff.service import ProcessObservation
from code_mule.state.store import JsonProjectStateStore
from state import make_project_state
from runtime_handoff.test_contracts import candidate


NOW = datetime(2026, 9, 12, tzinfo=UTC)


def git(root: Path, *args: str) -> str:
    return subprocess.run(("git",) + args, cwd=root, text=True, capture_output=True, check=True).stdout.strip()


class RuntimeHandoffE2E(unittest.TestCase):
    def _ready(self, root: Path, *, runnable: bool):
        git(root, "init")
        git(root, "config", "user.name", "Code Mule Test")
        git(root, "config", "user.email", "test@example.invalid")
        (root / ".git/info/exclude").write_text(".code-mule/\n", encoding="utf-8")
        if runnable:
            (root / "index.html").write_text("runtime handoff", encoding="utf-8")
            payload = candidate()
        else:
            (root / "src").mkdir()
            (root / "src/pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
            payload = candidate(kind="library", runnable=False)
        (root / "code-mule-delivery.json").write_text(__import__("json").dumps(payload), encoding="utf-8")
        git(root, "add", "--", ".")
        git(root, "commit", "-m", "deliver project")
        head = git(root, "rev-parse", "HEAD")
        manifest = parse_manifest_candidate(payload, project_id="project-1", revision_number=1, plan_version=1, generated_at=NOW)
        base = make_project_state()
        state = replace(
            base,
            project=replace(base.project, status=ProjectStatus.DONE, current_task_id=None, workspace=str(root)),
            plans=(replace(base.plans[0], status=PlanStatus.COMPLETED),),
            tasks=(replace(base.tasks[0], status=TaskStatus.COMPLETED),),
            revisions=(ProjectRevision(1, NOW, RevisionStatus.COMPLETED, "plan-1", 1, completed_at=NOW, completion_head=head, verification_status=RevisionCheckStatus.PASS, final_review_status=RevisionCheckStatus.PASS, verification_result_id="vr-1"),),
            delivery_manifest_required=True,
            delivery_manifests=(manifest,),
        )
        state_path = root / ".code-mule/project-state.json"
        state_path.parent.mkdir(parents=True)
        store = JsonProjectStateStore(state_path); store.save(state)
        alive = {43210: True}
        observation = ProcessObservation(True, "a" * 64, "b" * 64)
        class FakeProcess:
            pid = 43210
        def fake_popen(*args, **kwargs):
            return FakeProcess()
        def observe(pid):
            return observation if alive.get(pid, False) else ProcessObservation(False, None, None)
        def signal_process(pid, signal_number):
            alive[pid] = False
        service = RuntimeHandoffService(
            store=store, clock=lambda: NOW,
            session_id_factory=lambda: "runtime-1",
            event_id_factory=lambda: "event-runtime",
            environment={"PATH": __import__("os").environ["PATH"]},
            popen=fake_popen,
            process_observer=observe,
            http_status=lambda url, timeout: 200,
            process_signaler=signal_process,
            dynamic_port_factory=lambda: 48123,
            port_available=lambda port: True,
            sleeper=lambda seconds: None,
        )
        return store, service

    def test_real_local_static_server_launch_health_status_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, service = self._ready(root, runnable=True)
            outcome = service.launch()
            self.assertEqual(outcome.disposition, LaunchDisposition.STARTED)
            self.assertTrue(outcome.session.access_url.startswith("http://127.0.0.1:"))
            self.assertEqual(service.app_status().status.value, "running")
            self.assertEqual(service.stop_app().status.value, "stopped")
            self.assertEqual(git(root, "status", "--short"), "")
            self.assertEqual(len(store.load().runtime_sessions), 1)

    def test_library_handoff_never_starts_a_process(self):
        with tempfile.TemporaryDirectory() as directory:
            _, service = self._ready(Path(directory), runnable=False)
            outcome = service.launch()
            self.assertEqual(outcome.disposition, LaunchDisposition.NOT_RUNNABLE)
            self.assertIsNone(outcome.session)

    def test_project_not_done_and_unverified_manifest_cannot_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            store, service = self._ready(Path(directory), runnable=True)
            state = store.load(); store.save(replace(state, project=replace(state.project, status=ProjectStatus.RUNNING)))
            with self.assertRaises(RuntimeLaunchBlocked):
                service.launch()
            state = store.load()
            invalid = replace(state.delivery_manifests[0], status=DeliveryManifestStatus.INVALID, verified_at=None)
            store.save(replace(state, project=replace(state.project, status=ProjectStatus.DONE), delivery_manifests=(invalid,)))
            with self.assertRaises(RuntimeLaunchBlocked):
                service.launch()

    def test_dirty_completed_workspace_fails_closed_without_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, service = self._ready(root, runnable=True)
            (root / "unrelated.txt").write_text("dirty", encoding="utf-8")
            with self.assertRaises(RuntimeLaunchBlocked):
                service.launch()

    def test_missing_required_environment_blocks_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store, service = self._ready(root, runnable=True)
            state = store.load(); launch = replace(state.delivery_manifests[0].launch_spec, environment_keys=("MISSING_REQUIRED",))
            manifest = replace(state.delivery_manifests[0], launch_spec=launch, required_environment=("MISSING_REQUIRED",))
            store.save(replace(state, delivery_manifests=(manifest,)))
            with self.assertRaises(RuntimeLaunchBlocked):
                service.launch()

    def test_requires_arguments_returns_usage_without_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store, service = self._ready(root, runnable=True)
            state = store.load(); manifest = state.delivery_manifests[0]
            store.save(replace(state, delivery_manifests=(replace(manifest, launch_spec=replace(manifest.launch_spec, requires_args=True)),)))
            outcome = service.launch()
            self.assertEqual(outcome.disposition, LaunchDisposition.REQUIRES_ARGUMENTS)
            self.assertEqual(store.load().runtime_sessions, ())

    def test_fixed_port_conflict_does_not_touch_existing_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store, _ = self._ready(root, runnable=True)
            state = store.load(); manifest = state.delivery_manifests[0]
            launch = replace(manifest.launch_spec, supports_dynamic_port=False)
            manifest = replace(manifest, launch_spec=launch, access_spec=replace(manifest.access_spec, port=48000))
            store.save(replace(state, delivery_manifests=(manifest,)))
            calls = []
            service = RuntimeHandoffService(store=store, clock=lambda: NOW, session_id_factory=lambda: "r", event_id_factory=lambda: "e", environment={"PATH": __import__("os").environ["PATH"]}, port_available=lambda port: False, process_signaler=lambda *args: calls.append(args))
            with self.assertRaises(RuntimeLaunchBlocked):
                service.launch()
            self.assertEqual(calls, [])

    def test_pid_identity_mismatch_blocks_stop_without_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store, service = self._ready(root, runnable=True)
            service.launch(); state = store.load(); session = state.runtime_sessions[-1]
            store.save(replace(state, runtime_sessions=(replace(session, command_fingerprint="c" * 64),)))
            with self.assertRaises(RuntimeOwnershipUncertain):
                service.stop_app()
            self.assertEqual(store.load().runtime_sessions[-1].status.value, "ownership_uncertain")

    def test_new_composition_observes_exited_process_from_persisted_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store, service = self._ready(root, runnable=True)
            service.launch()
            reconstructed = RuntimeHandoffService(store=store, clock=lambda: NOW, session_id_factory=lambda: "runtime-2", event_id_factory=lambda: "event-2", environment={}, process_observer=lambda pid: ProcessObservation(False, None, None))
            self.assertEqual(reconstructed.app_status().status.value, "exited")

    def test_manifest_for_active_completed_revision_is_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store, service = self._ready(root, runnable=False)
            state = store.load(); old = replace(state.delivery_manifests[0], id="old", revision_number=1, plan_version=1, usage="old")
            current = replace(state.delivery_manifests[0], id="current", revision_number=2, plan_version=2, usage="current")
            revision = replace(state.revisions[0], revision_number=2, plan_version=2)
            store.save(replace(state, revisions=(replace(state.revisions[0], lifecycle_status=RevisionStatus.COMPLETED), revision), delivery_manifests=(old, current)))
            self.assertEqual(service.deliverable().id, "current")


if __name__ == "__main__":
    unittest.main()
