"""Read-only schema compatibility gate shared by stateful CLI surfaces."""

import json
from pathlib import Path

from code_mule.state.serialization import (
    StateSchemaCompatibility,
    StateSchemaCompatibilityCode,
    StateSchemaCompatibilityError,
    inspect_state_schema,
)


def inspect_state_file_schema(path: Path) -> StateSchemaCompatibility:
    """Read a state document and inspect only its root schema marker."""

    try:
        with path.open("r", encoding="utf-8") as stream:
            payload: object = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise StateSchemaCompatibilityError(
            StateSchemaCompatibility(
                StateSchemaCompatibilityCode.STATE_DOCUMENT_CORRUPT, None
            ),
            "project state file is not valid JSON",
        ) from error
    compatibility = inspect_state_schema(payload)
    if compatibility.compatible:
        return compatibility
    messages = {
        StateSchemaCompatibilityCode.CLI_TOO_OLD: "project state schema is newer than this CLI",
        StateSchemaCompatibilityCode.STATE_SCHEMA_UNSUPPORTED: "project state schema is older than this CLI supports",
        StateSchemaCompatibilityCode.STATE_SCHEMA_MISSING: "project state schema marker is missing",
        StateSchemaCompatibilityCode.STATE_SCHEMA_INVALID: "project state schema marker is invalid",
    }
    raise StateSchemaCompatibilityError(
        compatibility,
        messages.get(compatibility.code, "project state schema is invalid"),
    )


def command_requires_state_preflight(arguments: object) -> bool:
    command = getattr(arguments, "command", None)
    if command in {"init", "doctor", "version"}:
        return False
    if command == "ui" and getattr(arguments, "demo", False):
        return False
    return True


__all__ = ["command_requires_state_preflight", "inspect_state_file_schema"]
