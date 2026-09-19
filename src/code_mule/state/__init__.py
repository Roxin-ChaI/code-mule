"""Public contracts for persisted Code Mule project state."""

from .models import ProjectState
from .serialization import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    InvalidProjectState,
    StateSchemaCompatibility,
    StateSchemaCompatibilityCode,
    StateSchemaCompatibilityError,
    UnsupportedStateSchema,
    deserialize_project_state,
    serialize_project_state,
)
from .store import JsonProjectStateStore, ProjectStateNotFound

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "InvalidProjectState",
    "JsonProjectStateStore",
    "ProjectState",
    "ProjectStateNotFound",
    "StateSchemaCompatibility",
    "StateSchemaCompatibilityCode",
    "StateSchemaCompatibilityError",
    "UnsupportedStateSchema",
    "deserialize_project_state",
    "serialize_project_state",
]
