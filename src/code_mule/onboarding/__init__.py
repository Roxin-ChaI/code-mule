"""Deterministic environment checks and simplified project start."""

from .contracts import (
    DoctorCheck,
    DoctorReport,
    StartDecision,
    StartPreflight,
    WorkspaceProbe,
)
from .render import render_doctor
from .service import (
    DoctorService,
    StartPreflightService,
    run_local_command,
    safe_version,
    workspace_probe,
    workspace_slug,
)

__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "DoctorService",
    "StartDecision",
    "StartPreflight",
    "StartPreflightService",
    "WorkspaceProbe",
    "render_doctor",
    "run_local_command",
    "safe_version",
    "workspace_probe",
    "workspace_slug",
]
