"""Strict validation and materialization for delivery manifest candidates."""

from datetime import datetime
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse

from .contracts import (
    AccessSpec,
    CommandSpec,
    DeliverableType,
    DeliveryManifest,
    DeliveryManifestStatus,
    HealthCheckSpec,
    HealthCheckType,
    InvalidDeliveryManifest,
    LaunchSpec,
    StopSpec,
    VerificationSpec,
)


MANIFEST_FILE = "code-mule-delivery.json"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]+|(?:api[_-]?key|token|password|credential|authorization)\s*[:=])"
)
_FORBIDDEN_EXECUTABLES = frozenset(
    {
        "sudo", "su", "rm", "sh", "bash", "zsh", "fish", "eval",
        "ssh", "scp", "rsync", "curl", "wget", "nc", "ncat", "gh",
        "aws", "gcloud", "az", "kubectl", "helm", "terraform",
    }
)
_FORBIDDEN_GIT = frozenset(
    {"push", "reset", "clean", "tag", "checkout", "restore", "rebase"}
)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise InvalidDeliveryManifest(f"{context} must be an object")
    return value


def _exact(value: dict[str, object], fields: set[str], context: str) -> None:
    if set(value) != fields:
        raise InvalidDeliveryManifest(f"{context} fields do not match the contract")


def _string(value: object, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 600 or "\x00" in value:
        raise InvalidDeliveryManifest(f"{context} must be a bounded string")
    if not allow_empty and not value:
        raise InvalidDeliveryManifest(f"{context} must not be empty")
    if _SECRET.search(value):
        raise InvalidDeliveryManifest(f"{context} contains secret-like data")
    return value


def _strings(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise InvalidDeliveryManifest(f"{context} must be a bounded array")
    result = tuple(_string(item, context) for item in value)
    if len(set(result)) != len(result):
        raise InvalidDeliveryManifest(f"{context} must not contain duplicates")
    return result


def _bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise InvalidDeliveryManifest(f"{context} must be boolean")
    return value


def _number(value: object, context: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidDeliveryManifest(f"{context} must be numeric")
    return float(value)


def _relative(value: str, context: str) -> Path:
    path = Path(value)
    if path.is_absolute() or value in {"", ".", ".."} or ".." in path.parts:
        raise InvalidDeliveryManifest(f"{context} must be repository-relative")
    return path


def _command(value: object, context: str) -> CommandSpec:
    payload = _object(value, context)
    _exact(payload, {"executable", "args"}, context)
    command = CommandSpec(
        _string(payload["executable"], f"{context}.executable"),
        _strings(payload["args"], f"{context}.args"),
    )
    executable = Path(command.executable).name.lower()
    if executable in _FORBIDDEN_EXECUTABLES:
        raise InvalidDeliveryManifest("launch command is not allowed")
    if executable == "git" and command.args and command.args[0].lower() in _FORBIDDEN_GIT:
        raise InvalidDeliveryManifest("mutating Git command is not allowed")
    lowered = tuple(arg.lower() for arg in command.args)
    if executable.startswith("python") and any(item in {"-c", "-mhttp.client"} for item in lowered):
        raise InvalidDeliveryManifest("inline runtime evaluation is not allowed")
    if executable in {"node", "nodejs"} and any(item in {"-e", "--eval"} for item in lowered):
        raise InvalidDeliveryManifest("inline runtime evaluation is not allowed")
    if any(item in {"--force", "-f"} for item in lowered) and executable in {"git", "docker", "kubectl"}:
        raise InvalidDeliveryManifest("force operation is not allowed")
    return command


def _launch(value: object) -> LaunchSpec | None:
    if value is None:
        return None
    payload = _object(value, "launch_spec")
    _exact(
        payload,
        {
            "command",
            "working_directory",
            "environment_keys",
            "startup_timeout_seconds",
            "expected_long_running",
            "requires_args",
            "supports_dynamic_port",
        },
        "launch_spec",
    )
    return LaunchSpec(
        _command(payload["command"], "launch_spec.command"),
        _string(payload["working_directory"], "launch_spec.working_directory"),
        _strings(payload["environment_keys"], "launch_spec.environment_keys"),
        _number(payload["startup_timeout_seconds"], "startup_timeout_seconds"),
        _bool(payload["expected_long_running"], "expected_long_running"),
        _bool(payload["requires_args"], "requires_args"),
        _bool(payload["supports_dynamic_port"], "supports_dynamic_port"),
    )


def _verification(value: object) -> VerificationSpec:
    payload = _object(value, "verification_spec")
    _exact(payload, {"required_paths", "launch_smoke_test_supported"}, "verification_spec")
    return VerificationSpec(
        _strings(payload["required_paths"], "verification_spec.required_paths"),
        _bool(payload["launch_smoke_test_supported"], "launch_smoke_test_supported"),
    )


def _health(value: object) -> HealthCheckSpec:
    payload = _object(value, "health_check_spec")
    _exact(payload, {"type", "url", "command", "expected_status", "timeout_seconds"}, "health_check_spec")
    try:
        kind = HealthCheckType(_string(payload["type"], "health_check_spec.type"))
    except ValueError as error:
        raise InvalidDeliveryManifest("health check type is invalid") from error
    url = None if payload["url"] is None else _string(payload["url"], "health_check_spec.url")
    command = None if payload["command"] is None else _command(payload["command"], "health_check_spec.command")
    expected = payload["expected_status"]
    if expected is not None and (type(expected) is not int):
        raise InvalidDeliveryManifest("expected_status must be an integer or null")
    spec = HealthCheckSpec(
        kind,
        url,
        command,
        expected,
        _number(payload["timeout_seconds"], "health_check_spec.timeout_seconds"),
    )
    if spec.type is HealthCheckType.HTTP:
        parsed = urlparse(spec.url or "")
        if parsed.scheme != "http" or parsed.hostname not in _LOCAL_HOSTS:
            raise InvalidDeliveryManifest("HTTP health check must use local plain HTTP")
    return spec


def _access(value: object) -> AccessSpec | None:
    if value is None:
        return None
    payload = _object(value, "access_spec")
    _exact(payload, {"host", "port", "path"}, "access_spec")
    host = None if payload["host"] is None else _string(payload["host"], "access_spec.host")
    port = payload["port"]
    if port is not None and type(port) is not int:
        raise InvalidDeliveryManifest("access_spec.port must be an integer or null")
    access = AccessSpec(host, port, _string(payload["path"], "access_spec.path"))
    if access.host not in _LOCAL_HOSTS:
        raise InvalidDeliveryManifest("access host must be localhost")
    return access


def _stop(value: object) -> StopSpec | None:
    if value is None:
        return None
    payload = _object(value, "stop_spec")
    _exact(payload, {"grace_seconds"}, "stop_spec")
    return StopSpec(_number(payload["grace_seconds"], "stop_spec.grace_seconds"))


def _parse_manifest_candidate(
    payload: object,
    *,
    project_id: str,
    revision_number: int,
    plan_version: int,
    generated_at: datetime,
) -> DeliveryManifest:
    root = _object(payload, "delivery manifest")
    _exact(
        root,
        {
            "deliverable_type",
            "runnable",
            "entry_point",
            "launch_spec",
            "verification_spec",
            "health_check_spec",
            "access_spec",
            "stop_spec",
            "required_environment",
            "runtime_generated_paths",
            "usage",
        },
        "delivery manifest",
    )
    try:
        deliverable_type = DeliverableType(
            _string(root["deliverable_type"], "deliverable_type")
        )
    except ValueError as error:
        raise InvalidDeliveryManifest("deliverable_type is invalid") from error
    manifest = DeliveryManifest(
        id=f"manifest-r{revision_number}-{plan_version}",
        project_id=project_id,
        revision_number=revision_number,
        plan_version=plan_version,
        deliverable_type=deliverable_type,
        runnable=_bool(root["runnable"], "runnable"),
        entry_point=_string(root["entry_point"], "entry_point"),
        launch_spec=_launch(root["launch_spec"]),
        verification_spec=_verification(root["verification_spec"]),
        health_check_spec=_health(root["health_check_spec"]),
        access_spec=_access(root["access_spec"]),
        stop_spec=_stop(root["stop_spec"]),
        required_environment=_strings(root["required_environment"], "required_environment"),
        runtime_generated_paths=_strings(root["runtime_generated_paths"], "runtime_generated_paths"),
        usage=_string(root["usage"], "usage"),
        generated_at=generated_at,
        verified_at=generated_at,
        status=DeliveryManifestStatus.VERIFIED,
    )
    if manifest.deliverable_type is DeliverableType.UNKNOWN and manifest.runnable:
        raise InvalidDeliveryManifest("UNKNOWN deliverable cannot be runnable")
    if manifest.runnable and manifest.launch_spec is None:
        raise InvalidDeliveryManifest("runnable deliverable requires launch_spec")
    if not manifest.runnable and manifest.launch_spec is not None:
        raise InvalidDeliveryManifest("non-runnable deliverable cannot contain launch_spec")
    return manifest


def parse_manifest_candidate(
    payload: object,
    *,
    project_id: str,
    revision_number: int,
    plan_version: int,
    generated_at: datetime,
) -> DeliveryManifest:
    try:
        return _parse_manifest_candidate(
            payload,
            project_id=project_id,
            revision_number=revision_number,
            plan_version=plan_version,
            generated_at=generated_at,
        )
    except InvalidDeliveryManifest:
        raise
    except (TypeError, ValueError) as error:
        raise InvalidDeliveryManifest("delivery manifest violates its typed contract") from error


def validate_manifest(manifest: DeliveryManifest, workspace: Path) -> None:
    """Validate one already typed manifest against its exact local workspace."""

    root = workspace.resolve()
    if not root.is_dir():
        raise InvalidDeliveryManifest("workspace does not exist")
    entry = _relative(manifest.entry_point, "entry_point")
    if not (root / entry).exists():
        raise InvalidDeliveryManifest("delivery entry point does not exist")
    for path in manifest.verification_spec.required_paths:
        if not (root / _relative(path, "required path")).exists():
            raise InvalidDeliveryManifest("required delivery path does not exist")
    for path in manifest.runtime_generated_paths:
        _relative(path, "runtime generated path")
    if manifest.deliverable_type is DeliverableType.UNKNOWN:
        if manifest.runnable:
            raise InvalidDeliveryManifest("UNKNOWN deliverable cannot be runnable")
    if manifest.runnable:
        if manifest.launch_spec is None:
            raise InvalidDeliveryManifest("runnable deliverable requires launch_spec")
        working = manifest.launch_spec.working_directory
        working_path = root if working == "." else root / _relative(working, "working_directory")
        if not working_path.is_dir():
            raise InvalidDeliveryManifest("launch working directory does not exist")
        executable = manifest.launch_spec.command.executable
        candidate = Path(executable)
        if candidate.is_absolute():
            if not candidate.is_file():
                raise InvalidDeliveryManifest("launch executable does not exist")
        elif "/" in executable:
            if not (working_path / _relative(executable, "executable")).is_file():
                raise InvalidDeliveryManifest("launch executable does not exist")
        elif shutil.which(executable) is None:
            raise InvalidDeliveryManifest("launch executable is unavailable")
        if manifest.launch_spec.supports_dynamic_port:
            if manifest.access_spec is None or manifest.access_spec.port is not None:
                raise InvalidDeliveryManifest("dynamic port launch requires runtime-assigned access port")
            values = manifest.launch_spec.command.args
            if not any("{port}" in value for value in values):
                raise InvalidDeliveryManifest("dynamic port launch must declare a {port} argument")
    elif manifest.launch_spec is not None:
        raise InvalidDeliveryManifest("non-runnable deliverable cannot contain launch_spec")
    if manifest.health_check_spec.type is HealthCheckType.HTTP:
        parsed = urlparse(manifest.health_check_spec.url or "")
        if parsed.scheme != "http" or parsed.hostname not in _LOCAL_HOSTS:
            raise InvalidDeliveryManifest("HTTP health check must use local plain HTTP")
    if manifest.access_spec is not None and manifest.access_spec.host not in _LOCAL_HOSTS:
        raise InvalidDeliveryManifest("access host must be localhost")


def load_and_validate_manifest(
    workspace: Path,
    *,
    project_id: str,
    revision_number: int,
    plan_version: int,
    generated_at: datetime,
) -> DeliveryManifest:
    path = workspace / MANIFEST_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidDeliveryManifest(
            f"verified delivery manifest is unavailable at {MANIFEST_FILE}"
        ) from error
    manifest = parse_manifest_candidate(
        payload,
        project_id=project_id,
        revision_number=revision_number,
        plan_version=plan_version,
        generated_at=generated_at,
    )
    validate_manifest(manifest, workspace)
    return manifest


__all__ = [
    "MANIFEST_FILE",
    "load_and_validate_manifest",
    "parse_manifest_candidate",
    "validate_manifest",
]
