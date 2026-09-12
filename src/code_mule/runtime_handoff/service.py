"""Verified local launch, health, status, and graceful stop boundaries."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
from typing import Protocol
from urllib.request import ProxyHandler, build_opener

from code_mule.domain import ProjectEvent, ProjectStatus
from code_mule.revision import completed_revision
from code_mule.state.models import ProjectState

from .contracts import (
    DeliveryManifest,
    DeliveryManifestStatus,
    HealthCheckType,
    LaunchDisposition,
    LaunchResult,
    RuntimeHealthStatus,
    RuntimeLaunchBlocked,
    RuntimeOwnershipUncertain,
    RuntimePortConflict,
    RuntimeSession,
    RuntimeSessionStatus,
)
from .validation import validate_manifest


_BASE_ENVIRONMENT = frozenset({"PATH", "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH"})


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...
    def save(self, state: ProjectState) -> None: ...


@dataclass(frozen=True)
class ProcessObservation:
    alive: bool
    process_start_identity: str | None
    command_fingerprint: str | None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def observe_process(pid: int) -> ProcessObservation:
    """Read bounded OS process identity without trusting PID reuse."""
    try:
        start = subprocess.run(
            ("ps", "-p", str(pid), "-o", "lstart="),
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        command = subprocess.run(
            ("ps", "-ww", "-p", str(pid), "-o", "command="),
            text=True, capture_output=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ProcessObservation(False, None, None)
    if not start or not command:
        return ProcessObservation(False, None, None)
    return ProcessObservation(True, _fingerprint(start), _fingerprint(command))


def _http_status(url: str, timeout: float) -> int | None:
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=timeout) as response:
            return response.status
    except OSError:
        return None


def _dynamic_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _local_port_available(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class RuntimeHandoffService:
    """Launch only a verified completed revision and retain exact process identity."""

    def __init__(
        self,
        *,
        store: ProjectStateStore,
        clock: Callable[[], datetime],
        session_id_factory: Callable[[], str],
        event_id_factory: Callable[[], str],
        environment: Mapping[str, str] | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        process_observer: Callable[[int], ProcessObservation] = observe_process,
        http_status: Callable[[str, float], int | None] = _http_status,
        process_signaler: Callable[[int, int], None] = os.kill,
        dynamic_port_factory: Callable[[], int] = _dynamic_local_port,
        port_available: Callable[[int], bool] = _local_port_available,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._clock = clock
        self._session_id_factory = session_id_factory
        self._event_id_factory = event_id_factory
        self._environment = dict(os.environ if environment is None else environment)
        self._popen = popen
        self._observe = process_observer
        self._http_status = http_status
        self._signal = process_signaler
        self._dynamic_port = dynamic_port_factory
        self._port_available = port_available
        self._sleep = sleeper

    def deliverable(self) -> DeliveryManifest | None:
        state = self._store.load()
        return self._current_manifest(state, required=False)

    def launch(self) -> LaunchResult:
        state = self._store.load()
        manifest = self._current_manifest(state, required=True)
        assert manifest is not None
        workspace = self._verified_workspace(state, manifest)
        if not manifest.runnable:
            return LaunchResult(LaunchDisposition.NOT_RUNNABLE, manifest)
        launch = manifest.launch_spec
        assert launch is not None
        if launch.requires_args:
            return LaunchResult(LaunchDisposition.REQUIRES_ARGUMENTS, manifest)
        active = tuple(
            session for session in state.runtime_sessions
            if session.status in {RuntimeSessionStatus.STARTING, RuntimeSessionStatus.RUNNING}
        )
        if active:
            raise RuntimeLaunchBlocked("a runtime session is already active")
        missing = tuple(
            key for key in set(manifest.required_environment) | set(launch.environment_keys)
            if not self._environment.get(key)
        )
        if missing:
            raise RuntimeLaunchBlocked("required launch environment is unavailable")
        for generated in manifest.runtime_generated_paths:
            ignored = subprocess.run(
                ("git", "check-ignore", "--quiet", "--", generated),
                cwd=workspace, check=False,
            ).returncode == 0
            if not ignored:
                raise RuntimeLaunchBlocked("runtime-generated path is not Git ignored")
        port = self._select_port(manifest)
        argv = self._command_argv(launch.command.executable, launch.command.args, workspace, launch.working_directory, port)
        cwd = workspace if launch.working_directory == "." else workspace / launch.working_directory
        session_id = self._session_id_factory()
        safe_environment = {
            key: value for key, value in self._environment.items()
            if key in _BASE_ENVIRONMENT or key in launch.environment_keys
        }
        try:
            process = self._popen(
                argv, cwd=cwd, env=safe_environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, start_new_session=True,
            )
        except OSError as error:
            raise RuntimeLaunchBlocked("verified launch command could not start") from error
        if not launch.expected_long_running:
            try:
                exit_code = process.wait(timeout=launch.startup_timeout_seconds)
            except subprocess.TimeoutExpired as error:
                process.terminate()
                raise RuntimeLaunchBlocked("finite launch exceeded its bounded runtime") from error
            session = RuntimeSession(
                id=session_id, project_id=state.project.id,
                revision_number=manifest.revision_number, manifest_id=manifest.id,
                pid=process.pid, started_at=self._clock(),
                status=RuntimeSessionStatus.EXITED if exit_code == 0 else RuntimeSessionStatus.FAILED,
                access_url=None,
                health_status=RuntimeHealthStatus.HEALTHY if exit_code == 0 else RuntimeHealthStatus.UNHEALTHY,
                process_start_identity=None, command_fingerprint=None, exit_code=exit_code,
            )
            self._persist_session(state, session, "runtime.completed")
            if exit_code != 0:
                raise RuntimeLaunchBlocked("finite launch command failed")
            return LaunchResult(LaunchDisposition.COMPLETED, manifest, session)
        observation = self._wait_for_identity(process.pid, launch.startup_timeout_seconds)
        if not observation.alive:
            if hasattr(process, "terminate"):
                process.terminate()
            raise RuntimeLaunchBlocked("runtime exited during startup")
        access_url = self._access_url(manifest, port)
        session = RuntimeSession(
            id=session_id, project_id=state.project.id,
            revision_number=manifest.revision_number, manifest_id=manifest.id,
            pid=process.pid, started_at=self._clock(), status=RuntimeSessionStatus.STARTING,
            access_url=access_url, health_status=RuntimeHealthStatus.NOT_CHECKED,
            process_start_identity=observation.process_start_identity,
            command_fingerprint=observation.command_fingerprint,
        )
        state = self._persist_session(state, session, "runtime.started")
        health = self._wait_for_health(manifest, session, workspace, port)
        accepted_health = health in {RuntimeHealthStatus.HEALTHY, RuntimeHealthStatus.NOT_CHECKED}
        session = replace(
            session,
            status=RuntimeSessionStatus.RUNNING if accepted_health else RuntimeSessionStatus.FAILED,
            health_status=health,
        )
        self._replace_session(self._store.load(), session, "runtime.health_checked")
        if not accepted_health:
            latest_observation = self._observe(session.pid or 0)
            if latest_observation.alive and self._owned(session, latest_observation):
                self._signal(session.pid or 0, signal.SIGTERM)
            raise RuntimeLaunchBlocked("runtime health check failed")
        return LaunchResult(LaunchDisposition.STARTED, manifest, session)

    def app_status(self) -> RuntimeSession | None:
        state = self._store.load()
        if not state.runtime_sessions:
            return None
        session = state.runtime_sessions[-1]
        if session.status not in {RuntimeSessionStatus.STARTING, RuntimeSessionStatus.RUNNING}:
            return session
        observation = self._observe(session.pid or 0)
        if not observation.alive:
            updated = replace(session, status=RuntimeSessionStatus.EXITED, health_status=RuntimeHealthStatus.UNHEALTHY)
        elif not self._owned(session, observation):
            updated = replace(session, status=RuntimeSessionStatus.OWNERSHIP_UNCERTAIN, health_status=RuntimeHealthStatus.UNHEALTHY)
        else:
            updated = session
        if updated != session:
            self._replace_session(state, updated, "runtime.status_changed")
        return updated

    def stop_app(self) -> RuntimeSession:
        state = self._store.load()
        if not state.runtime_sessions:
            raise RuntimeLaunchBlocked("no runtime session exists")
        session = state.runtime_sessions[-1]
        if session.status in {RuntimeSessionStatus.EXITED, RuntimeSessionStatus.STOPPED, RuntimeSessionStatus.FAILED}:
            return session
        observation = self._observe(session.pid or 0)
        if not observation.alive:
            updated = replace(session, status=RuntimeSessionStatus.EXITED, health_status=RuntimeHealthStatus.UNHEALTHY)
            self._replace_session(state, updated, "runtime.status_changed")
            return updated
        if not self._owned(session, observation):
            updated = replace(session, status=RuntimeSessionStatus.OWNERSHIP_UNCERTAIN)
            self._replace_session(state, updated, "runtime.ownership_uncertain")
            raise RuntimeOwnershipUncertain("runtime process identity does not match")
        manifest = next(item for item in state.delivery_manifests if item.id == session.manifest_id)
        deadline = time.monotonic() + (manifest.stop_spec.grace_seconds if manifest.stop_spec else 5.0)
        self._signal(session.pid or 0, signal.SIGTERM)
        while time.monotonic() < deadline and self._observe(session.pid or 0).alive:
            self._sleep(0.1)
        if self._observe(session.pid or 0).alive:
            raise RuntimeLaunchBlocked("runtime did not stop within its graceful boundary")
        updated = replace(
            session, status=RuntimeSessionStatus.STOPPED,
            health_status=RuntimeHealthStatus.UNHEALTHY,
            stop_requested_at=self._clock(),
        )
        self._replace_session(state, updated, "runtime.stopped")
        return updated

    def _current_manifest(self, state: ProjectState, *, required: bool) -> DeliveryManifest | None:
        revision = completed_revision(state)
        if state.project.status is not ProjectStatus.DONE or revision is None:
            if required:
                raise RuntimeLaunchBlocked("only a completed revision can be handed off")
            return None
        matching = tuple(
            item for item in state.delivery_manifests
            if item.revision_number == revision.revision_number
            and item.plan_version == revision.plan_version
            and item.status is DeliveryManifestStatus.VERIFIED
        )
        if len(matching) != 1:
            if required:
                raise RuntimeLaunchBlocked("current revision has no unique verified manifest")
            return None
        return matching[0]

    def _verified_workspace(self, state: ProjectState, manifest: DeliveryManifest) -> Path:
        revision = completed_revision(state)
        if revision is None or not revision.completion_head or state.project.workspace is None:
            raise RuntimeLaunchBlocked("completed revision evidence is unavailable")
        workspace = Path(state.project.workspace).resolve()
        validate_manifest(manifest, workspace)
        try:
            root = subprocess.run(("git", "rev-parse", "--show-toplevel"), cwd=workspace, text=True, capture_output=True, check=True).stdout.strip()
            head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=workspace, text=True, capture_output=True, check=True).stdout.strip()
            dirty = subprocess.run(("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=workspace, text=True, capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeLaunchBlocked("workspace Git evidence is unavailable") from error
        if Path(root).resolve() != workspace or head != revision.completion_head or dirty:
            raise RuntimeLaunchBlocked("workspace no longer matches the completed revision")
        return workspace

    @staticmethod
    def _command_argv(executable: str, args: Sequence[str], workspace: Path, working_directory: str, port: int | None) -> tuple[str, ...]:
        cwd = workspace if working_directory == "." else workspace / working_directory
        if Path(executable).is_absolute():
            resolved = executable
        elif "/" in executable:
            resolved = str((cwd / executable).resolve())
        else:
            resolved = shutil.which(executable) or executable
        replacement = "" if port is None else str(port)
        return (resolved,) + tuple(arg.replace("{port}", replacement) for arg in args)

    def _select_port(self, manifest: DeliveryManifest) -> int | None:
        if manifest.access_spec is None:
            return None
        port = manifest.access_spec.port
        if manifest.launch_spec and manifest.launch_spec.supports_dynamic_port:
            return self._dynamic_port()
        if port is not None and not self._port_available(port):
            raise RuntimePortConflict("configured local port is already in use")
        return port

    @staticmethod
    def _access_url(manifest: DeliveryManifest, port: int | None) -> str | None:
        if manifest.access_spec is None or port is None:
            return None
        return f"http://{manifest.access_spec.host}:{port}{manifest.access_spec.path}"

    def _wait_for_identity(self, pid: int, timeout: float) -> ProcessObservation:
        deadline = time.monotonic() + timeout
        observation = self._observe(pid)
        while not observation.alive and time.monotonic() < deadline:
            self._sleep(0.05)
            observation = self._observe(pid)
        return observation

    def _wait_for_health(self, manifest: DeliveryManifest, session: RuntimeSession, workspace: Path, port: int | None) -> RuntimeHealthStatus:
        spec = manifest.health_check_spec
        if spec.type is HealthCheckType.NONE:
            return RuntimeHealthStatus.NOT_CHECKED
        deadline = time.monotonic() + (manifest.launch_spec.startup_timeout_seconds if manifest.launch_spec else spec.timeout_seconds)
        while time.monotonic() < deadline:
            observation = self._observe(session.pid or 0)
            if not observation.alive or not self._owned(session, observation):
                return RuntimeHealthStatus.UNHEALTHY
            if spec.type is HealthCheckType.PROCESS:
                return RuntimeHealthStatus.HEALTHY
            if spec.type is HealthCheckType.HTTP:
                url = (spec.url or "").replace("{port}", "" if port is None else str(port))
                if self._http_status(url, spec.timeout_seconds) == spec.expected_status:
                    return RuntimeHealthStatus.HEALTHY
            elif spec.type is HealthCheckType.COMMAND and spec.command is not None:
                argv = self._command_argv(spec.command.executable, spec.command.args, workspace, ".", port)
                launch_keys = () if manifest.launch_spec is None else manifest.launch_spec.environment_keys
                environment = {
                    key: value for key, value in self._environment.items()
                    if key in _BASE_ENVIRONMENT or key in launch_keys
                }
                try:
                    healthy = subprocess.run(
                        argv, cwd=workspace, env=environment,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, shell=False, check=False,
                        timeout=spec.timeout_seconds,
                    ).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    healthy = False
                if healthy:
                    return RuntimeHealthStatus.HEALTHY
            self._sleep(0.1)
        return RuntimeHealthStatus.UNHEALTHY

    @staticmethod
    def _owned(session: RuntimeSession, observation: ProcessObservation) -> bool:
        return (
            observation.process_start_identity == session.process_start_identity
            and observation.command_fingerprint == session.command_fingerprint
        )

    def _persist_session(self, state: ProjectState, session: RuntimeSession, event_type: str) -> ProjectState:
        updated = replace(
            state,
            runtime_sessions=state.runtime_sessions + (session,),
            events=state.events + (self._event(state, event_type, session),),
        )
        self._store.save(updated)
        return updated

    def _replace_session(self, state: ProjectState, session: RuntimeSession, event_type: str) -> ProjectState:
        updated = replace(
            state,
            runtime_sessions=tuple(session if item.id == session.id else item for item in state.runtime_sessions),
            events=state.events + (self._event(state, event_type, session),),
        )
        self._store.save(updated)
        return updated

    def _event(self, state: ProjectState, event_type: str, session: RuntimeSession) -> ProjectEvent:
        return ProjectEvent(
            self._event_id_factory(), state.project.id, event_type, session.id,
            self._clock(),
            {"session_id": session.id, "status": session.status.value, "health": session.health_status.value},
        )


__all__ = ["ProcessObservation", "RuntimeHandoffService", "observe_process"]
