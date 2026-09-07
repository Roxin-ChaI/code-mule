"""Versioned project revision helpers and typed history records."""

from .contracts import (
    ProjectRevision,
    RevisionCheckStatus,
    RevisionStatus,
)
from .service import (
    begin_revision,
    complete_revision,
    completed_revision,
    latest_revision,
    next_revision_number,
)

__all__ = [
    "ProjectRevision",
    "RevisionCheckStatus",
    "RevisionStatus",
    "begin_revision",
    "complete_revision",
    "completed_revision",
    "latest_revision",
    "next_revision_number",
]
