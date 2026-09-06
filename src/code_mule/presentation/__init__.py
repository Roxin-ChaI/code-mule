"""Read-only Boss-facing presentation models and renderers."""

from .labels import decision_label, humanize_identifier, status_label
from .dashboard import render_dashboard
from .models import HumanActionView, ProjectView, human_action_view, project_view
from .render import (
    render_change_applied,
    render_change_requested,
    render_human_action,
    render_project,
    render_project_diagnosis,
    render_project_cancelled,
    render_project_cancellation_requested,
)

__all__ = [
    "HumanActionView",
    "ProjectView",
    "decision_label",
    "human_action_view",
    "humanize_identifier",
    "project_view",
    "render_change_applied",
    "render_change_requested",
    "render_human_action",
    "render_project",
    "render_project_diagnosis",
    "render_project_cancelled",
    "render_project_cancellation_requested",
    "render_dashboard",
    "status_label",
]
