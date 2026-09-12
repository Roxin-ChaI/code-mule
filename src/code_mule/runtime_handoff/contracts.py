"""Typed contracts for verified local delivery and runtime ownership."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re


_ID = re.compile(r"[^\s\x00-\x1f]{1,128}")
_ENVIRONMENT_KEY = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded identifier")


def _bounded(value: str, name: str, limit: int = 300) -> None:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} must be a bounded non-empty string")


def _strings(values: tuple[str, ...], name: str, *, limit: int = 240) -> None:
    if not isinstance(values, tuple) or len(values) > 100 or len(set(values)) != len(values):
        raise ValueError(f"{name} must be a bounded tuple without duplicates")
    for value in values:
        _bounded(value, name, limit)


class DeliverableType(StrEnum):
    CLI = "cli"
    SERVICE = "service"
    STATIC_WEB = "static_web"
    DESKTOP = "desktop"
    EXECUTABLE = "executable"
    LIBRARY = "library"
    COMPONENT = "component"
    PACKAGE = "package"
    DOCUMENTATION = "documentation"
    UNKNOWN = "unknown"


class DeliveryManifestStatus(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"


class HealthCheckType(StrEnum):
    PROCESS = "process"
    HTTP = "http"
    COMMAND = "command"
    NONE = "none"


class RuntimeSessionStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    STOPPED = "stopped"
    FAILED = "failed"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"


class RuntimeHealthStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    NOT_CHECKED = "not_checked"


class LaunchDisposition(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    NOT_RUNNABLE = "not_runnable"
    REQUIRES_ARGUMENTS = "requires_arguments"


@dataclass(frozen=True)
class CommandSpec:
    executable: str
    args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded(self.executable, "executable", 240)
        _strings(self.args, "args", limit=500)


@dataclass(frozen=True)
class LaunchSpec:
    command: CommandSpec
    working_directory: str
    environment_keys: tuple[str, ...]
    startup_timeout_seconds: float
    expected_long_running: bool
    requires_args: bool = False
    supports_dynamic_port: bool = False

    def __post_init__(self) -> None:
        _bounded(self.working_directory, "working_directory", 240)
        _strings(self.environment_keys, "environment_keys", limit=64)
        if any(_ENVIRONMENT_KEY.fullmatch(key) is None for key in self.environment_keys):
            raise ValueError("environment_keys must contain environment variable names")
        if not isinstance(self.startup_timeout_seconds, (int, float)) or not 0 < self.startup_timeout_seconds <= 120:
            raise ValueError("startup_timeout_seconds must be in (0, 120]")
        if type(self.expected_long_running) is not bool or type(self.requires_args) is not bool or type(self.supports_dynamic_port) is not bool:
            raise ValueError("launch flags must be booleans")


@dataclass(frozen=True)
class VerificationSpec:
    required_paths: tuple[str, ...]
    launch_smoke_test_supported: bool

    def __post_init__(self) -> None:
        _strings(self.required_paths, "required_paths")
        if type(self.launch_smoke_test_supported) is not bool:
            raise ValueError("launch_smoke_test_supported must be boolean")


@dataclass(frozen=True)
class HealthCheckSpec:
    type: HealthCheckType
    url: str | None = None
    command: CommandSpec | None = None
    expected_status: int | None = None
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.url is not None:
            _bounded(self.url, "health url", 500)
        if not isinstance(self.timeout_seconds, (int, float)) or not 0 < self.timeout_seconds <= 30:
            raise ValueError("health timeout must be in (0, 30]")
        if self.type is HealthCheckType.HTTP:
            if self.url is None or self.command is not None or self.expected_status is None:
                raise ValueError("HTTP health requires url and expected_status")
            if not 100 <= self.expected_status <= 599:
                raise ValueError("expected_status must be an HTTP status")
        elif self.type is HealthCheckType.COMMAND:
            if self.command is None or self.url is not None or self.expected_status is not None:
                raise ValueError("COMMAND health requires only a command")
        elif self.type in {HealthCheckType.PROCESS, HealthCheckType.NONE}:
            if self.url is not None or self.command is not None or self.expected_status is not None:
                raise ValueError("PROCESS/NONE health cannot contain command or URL")


@dataclass(frozen=True)
class AccessSpec:
    host: str | None = None
    port: int | None = None
    path: str = "/"

    def __post_init__(self) -> None:
        if self.host is not None:
            _bounded(self.host, "access host", 100)
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("access port must be in [1, 65535]")
        _bounded(self.path, "access path", 300)
        if not self.path.startswith("/"):
            raise ValueError("access path must start with /")


@dataclass(frozen=True)
class StopSpec:
    grace_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.grace_seconds, (int, float)) or not 0 < self.grace_seconds <= 30:
            raise ValueError("grace_seconds must be in (0, 30]")


@dataclass(frozen=True)
class DeliveryManifest:
    id: str
    project_id: str
    revision_number: int
    plan_version: int
    deliverable_type: DeliverableType
    runnable: bool
    entry_point: str
    launch_spec: LaunchSpec | None
    verification_spec: VerificationSpec
    health_check_spec: HealthCheckSpec
    access_spec: AccessSpec | None
    stop_spec: StopSpec | None
    required_environment: tuple[str, ...]
    runtime_generated_paths: tuple[str, ...]
    usage: str
    generated_at: datetime
    verified_at: datetime | None
    status: DeliveryManifestStatus

    def __post_init__(self) -> None:
        _identifier(self.id, "manifest id")
        _identifier(self.project_id, "project id")
        if self.revision_number < 1 or self.plan_version < 1:
            raise ValueError("manifest revision and Plan version must be positive")
        if type(self.runnable) is not bool:
            raise ValueError("runnable must be boolean")
        _bounded(self.entry_point, "entry_point", 240)
        _strings(self.required_environment, "required_environment", limit=64)
        if any(_ENVIRONMENT_KEY.fullmatch(key) is None for key in self.required_environment):
            raise ValueError("required_environment must contain variable names")
        _strings(self.runtime_generated_paths, "runtime_generated_paths")
        _bounded(self.usage, "usage", 600)
        if self.status is DeliveryManifestStatus.VERIFIED and self.verified_at is None:
            raise ValueError("verified manifest requires verified_at")


@dataclass(frozen=True)
class RuntimeSession:
    id: str
    project_id: str
    revision_number: int
    manifest_id: str
    pid: int | None
    started_at: datetime
    status: RuntimeSessionStatus
    access_url: str | None
    health_status: RuntimeHealthStatus
    process_start_identity: str | None
    command_fingerprint: str | None
    exit_code: int | None = None
    stop_requested_at: datetime | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "session id")
        _identifier(self.project_id, "project id")
        _identifier(self.manifest_id, "manifest id")
        if self.revision_number < 1:
            raise ValueError("session revision_number must be positive")
        if self.pid is not None and self.pid < 1:
            raise ValueError("pid must be positive")
        if self.access_url is not None:
            _bounded(self.access_url, "access_url", 500)
        for value, name in (
            (self.process_start_identity, "process_start_identity"),
            (self.command_fingerprint, "command_fingerprint"),
        ):
            if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} must be a SHA-256 fingerprint")


@dataclass(frozen=True)
class LaunchResult:
    disposition: LaunchDisposition
    manifest: DeliveryManifest
    session: RuntimeSession | None = None

    def __post_init__(self) -> None:
        has_session = self.disposition in {LaunchDisposition.STARTED, LaunchDisposition.COMPLETED}
        if has_session != (self.session is not None):
            raise ValueError("started/completed launches require a runtime session")


class RuntimeHandoffError(RuntimeError):
    """Fail-closed local runtime handoff error."""


class InvalidDeliveryManifest(RuntimeHandoffError):
    pass


class RuntimeLaunchBlocked(RuntimeHandoffError):
    pass


class RuntimeOwnershipUncertain(RuntimeHandoffError):
    pass


class RuntimePortConflict(RuntimeLaunchBlocked):
    pass


__all__ = [
    "AccessSpec",
    "CommandSpec",
    "DeliverableType",
    "DeliveryManifest",
    "DeliveryManifestStatus",
    "HealthCheckSpec",
    "HealthCheckType",
    "InvalidDeliveryManifest",
    "LaunchDisposition",
    "LaunchResult",
    "LaunchSpec",
    "RuntimeHandoffError",
    "RuntimeHealthStatus",
    "RuntimeLaunchBlocked",
    "RuntimeOwnershipUncertain",
    "RuntimePortConflict",
    "RuntimeSession",
    "RuntimeSessionStatus",
    "StopSpec",
    "VerificationSpec",
]
