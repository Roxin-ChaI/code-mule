"""Typed delivery-manifest handoff diagnostics.

One place names where the Worker candidate -> revision-scoped handoff ->
deterministic Final Verification chain stopped, so "missing or invalid" can
never hide a present-but-rejected manifest again.
"""

from dataclasses import dataclass
from enum import StrEnum

from .contracts import DeliverableType, InvalidDeliveryManifest


CANDIDATE_FILENAME = "code-mule-delivery.json"
DEFAULT_CANDIDATE_PATH = CANDIDATE_FILENAME


class ManifestFailureStage(StrEnum):
    """Where the handoff chain stopped."""

    CANDIDATE_LOOKUP = "candidate_lookup"
    JSON_DECODE = "json_decode"
    CONTRACT_PARSE = "contract_parse"
    WORKSPACE_VALIDATION = "workspace_validation"
    REVISION_OWNERSHIP = "revision_ownership"
    RUNTIME_SMOKE = "runtime_smoke"


class ManifestFailureCode(StrEnum):
    """Bounded reason a delivery manifest could not be verified."""

    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_UNREADABLE = "manifest_unreadable"
    MANIFEST_PARSE_FAILED = "manifest_parse_failed"
    MANIFEST_VALIDATION_FAILED = "manifest_validation_failed"
    MANIFEST_REVISION_MISMATCH = "manifest_revision_mismatch"
    MANIFEST_HANDOFF_FAILED = "manifest_handoff_failed"
    MANIFEST_RUNTIME_SMOKE_FAILED = "manifest_runtime_smoke_failed"


# Rejections that mean "the candidate exists but violates the contract".
_INVALID_CODES = frozenset(
    {
        ManifestFailureCode.MANIFEST_PARSE_FAILED,
        ManifestFailureCode.MANIFEST_VALIDATION_FAILED,
        ManifestFailureCode.MANIFEST_RUNTIME_SMOKE_FAILED,
    }
)


class ManifestHandoffError(InvalidDeliveryManifest):
    """A typed, bounded stop in the delivery manifest handoff chain."""

    def __init__(
        self,
        stage: ManifestFailureStage,
        code: ManifestFailureCode,
        *,
        field_path: str | None = None,
        candidate_path: str | None = DEFAULT_CANDIDATE_PATH,
        revision: int | None = None,
        summary: str | None = None,
    ) -> None:
        if not isinstance(stage, ManifestFailureStage):
            raise ValueError("manifest failure stage must be typed")
        if not isinstance(code, ManifestFailureCode):
            raise ValueError("manifest failure code must be typed")
        self.stage = stage
        self.code = code
        self.field_path = field_path
        self.candidate_path = candidate_path
        self.revision = revision
        self.summary = (summary or self._default_summary())[:300]
        super().__init__(self.summary)

    @property
    def candidate_present(self) -> bool:
        """Whether a candidate file was found at all."""

        return self.code is not ManifestFailureCode.MANIFEST_MISSING

    def _default_summary(self) -> str:
        where = "" if self.field_path is None else f" at {self.field_path}"
        return f"delivery manifest {self.stage.value}: {self.code.value}{where}"

    @property
    def message(self) -> str:
        """Boss-facing wording that never says "missing" for a real candidate."""

        if self.code is ManifestFailureCode.MANIFEST_MISSING:
            return "Delivery manifest candidate was not found"
        if self.code is ManifestFailureCode.MANIFEST_REVISION_MISMATCH:
            return "Delivery manifest does not belong to the active revision"
        if self.code in _INVALID_CODES:
            return "Delivery manifest candidate was rejected by verification"
        return "Delivery manifest handoff could not complete"

    @property
    def requested_action(self) -> str:
        if self.code is ManifestFailureCode.MANIFEST_MISSING:
            return f"Create {DEFAULT_CANDIDATE_PATH} at the repository root"
        if self.code is ManifestFailureCode.MANIFEST_REVISION_MISMATCH:
            return "Re-deliver the active revision with its own manifest candidate"
        if self.code is ManifestFailureCode.MANIFEST_RUNTIME_SMOKE_FAILED:
            return "Correct the local launch, health, or stop contract"
        if self.field_path is not None:
            return f"Correct {self.field_path} in {DEFAULT_CANDIDATE_PATH}"
        return f"Correct {DEFAULT_CANDIDATE_PATH} and re-run final verification"


def manifest_failure_metadata(error: InvalidDeliveryManifest) -> dict[str, str]:
    """Bounded, secret-free metadata for one manifest handoff failure."""

    if not isinstance(error, ManifestHandoffError):
        return {
            "error_type": type(error).__name__,
            "manifest_stage": "unknown",
            "manifest_code": ManifestFailureCode.MANIFEST_HANDOFF_FAILED.value,
        }
    metadata = {
        "error_type": type(error).__name__,
        "manifest_stage": error.stage.value,
        "manifest_code": error.code.value,
        "manifest_candidate_present": str(error.candidate_present).lower(),
    }
    if error.field_path is not None:
        metadata["manifest_field_path"] = error.field_path[:160]
    if error.candidate_path is not None:
        metadata["manifest_candidate_path"] = error.candidate_path[:240]
    if error.revision is not None:
        metadata["manifest_revision"] = str(error.revision)
    metadata["manifest_summary"] = error.summary[:300]
    return metadata


# Stage each known context failure maps to when it did not raise a typed error.
_CONTEXT_STAGES = {
    "active revision context is unavailable": (
        ManifestFailureStage.REVISION_OWNERSHIP,
        ManifestFailureCode.MANIFEST_HANDOFF_FAILED,
    ),
    "delivery manifest identity is ambiguous": (
        ManifestFailureStage.REVISION_OWNERSHIP,
        ManifestFailureCode.MANIFEST_REVISION_MISMATCH,
    ),
}


def as_handoff_error(error: InvalidDeliveryManifest) -> ManifestHandoffError:
    """Normalize any manifest failure into the typed handoff vocabulary."""

    if isinstance(error, ManifestHandoffError):
        return error
    stage, code = _CONTEXT_STAGES.get(
        str(error), (ManifestFailureStage.CONTRACT_PARSE, ManifestFailureCode.MANIFEST_HANDOFF_FAILED)
    )
    return ManifestHandoffError(
        stage, code, field_path=None, summary=str(error)[:300]
    )


def field_path_from_context(message: str) -> str | None:
    """Recover a dotted field path from a validator context message."""

    token = message.split(" ", 1)[0]
    candidate = token.rstrip(":")
    if (
        not candidate
        or len(candidate) > 120
        or candidate == "delivery"
        or all(part.isidentifier() for part in candidate.split(".")) is False
    ):
        return None
    return candidate


def manifest_field_path(payload: object, message: str) -> str | None:
    """Name the exact candidate field a contract rejection refers to."""

    if isinstance(payload, dict) and "fields do not match the contract" in message:
        required = set(manifest_candidate_contract().top_level_fields)
        extra = sorted(set(payload) - required)
        if extra:
            return extra[0]
        missing = sorted(required - set(payload))
        if missing:
            return missing[0]
    return field_path_from_context(message)


@dataclass(frozen=True)
class ManifestCandidateContract:
    """The exact wire contract the Worker candidate must satisfy."""

    top_level_fields: tuple[str, ...]
    deliverable_types: tuple[str, ...]
    service_example: dict[str, object]
    non_runnable_example: dict[str, object]


def manifest_candidate_contract() -> ManifestCandidateContract:
    """Single source of truth shared by the prompt and the validator."""

    return ManifestCandidateContract(
        top_level_fields=(
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
        ),
        deliverable_types=tuple(item.value for item in DeliverableType),
        service_example={
            "deliverable_type": "service",
            "runnable": True,
            "entry_point": "server.py",
            "launch_spec": {
                "command": {
                    "executable": "python3",
                    "args": ["server.py", "--host", "127.0.0.1", "--port", "{port}"],
                },
                "working_directory": ".",
                "environment_keys": [],
                "startup_timeout_seconds": 10,
                "expected_long_running": True,
                "requires_args": False,
                "supports_dynamic_port": True,
            },
            "verification_spec": {
                "required_paths": ["server.py", "tests/test_server.py"],
                "launch_smoke_test_supported": True,
            },
            "health_check_spec": {
                "type": "http",
                "url": "http://127.0.0.1:{port}/health",
                "command": None,
                "expected_status": 200,
                "timeout_seconds": 3,
            },
            "access_spec": {"host": "127.0.0.1", "port": None, "path": "/"},
            "stop_spec": {"grace_seconds": 5},
            "required_environment": [],
            "runtime_generated_paths": [".code-mule/runtime"],
            "usage": "Run code-mule launch, then GET the reported local URL.",
        },
        non_runnable_example={
            "deliverable_type": "library",
            "runnable": False,
            "entry_point": "src/pkg/__init__.py",
            "launch_spec": None,
            "verification_spec": {
                "required_paths": ["src/pkg/__init__.py"],
                "launch_smoke_test_supported": False,
            },
            "health_check_spec": {
                "type": "none",
                "url": None,
                "command": None,
                "expected_status": None,
                "timeout_seconds": 5,
            },
            "access_spec": None,
            "stop_spec": None,
            "required_environment": [],
            "runtime_generated_paths": [],
            "usage": "Import the package entry point.",
        },
    )


def manifest_candidate_prompt() -> str:
    """Exact, compact instruction block for the final delivery Task."""

    import json

    contract = manifest_candidate_contract()
    return (
        "\n\nFinal delivery handoff:\n"
        f"- Create {CANDIDATE_FILENAME} at the repository root for the actual deliverable.\n"
        "- Use exactly these top-level fields: "
        + ", ".join(contract.top_level_fields)
        + ".\n"
        "- deliverable_type must be one of: "
        + ", ".join(contract.deliverable_types)
        + ".\n"
        "- The nested shape is fixed. A runnable local service candidate must look "
        "exactly like this (values change, field names do not):\n"
        + json.dumps(contract.service_example, indent=1)
        + "\n"
        "- A non-runnable product uses this shape instead:\n"
        + json.dumps(contract.non_runnable_example, indent=1)
        + "\n"
        "- A command is an object with executable and args array; never a shell string. "
        'The literal "{port}" placeholder is substituted by Code Mule before launch.\n'
        "- Runnable products need scope-fixed localhost launch, health, access, and "
        "graceful-stop metadata. Non-runnable products must use runnable=false with "
        "launch_spec, access_spec, and stop_spec set to null.\n"
        "- Do not invent extra fields anywhere: the candidate is rejected wholesale "
        "when its fields do not match the contract exactly.\n"
        "- Do not include secrets. Runtime-generated paths must already be Git ignored.\n"
        "- Code Mule Final Verification alone performs launch, localhost health "
        "checking, and process stop; never claim to have run them.\n"
    )


__all__ = [
    "CANDIDATE_FILENAME",
    "DEFAULT_CANDIDATE_PATH",
    "ManifestCandidateContract",
    "ManifestFailureCode",
    "ManifestFailureStage",
    "ManifestHandoffError",
    "as_handoff_error",
    "field_path_from_context",
    "manifest_candidate_contract",
    "manifest_candidate_prompt",
    "manifest_field_path",
    "manifest_failure_metadata",
]
