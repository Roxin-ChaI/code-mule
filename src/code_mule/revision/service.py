"""Deterministic helpers for one linear ProjectRevision history."""

from dataclasses import replace
from datetime import datetime

from code_mule.domain.enums import RevisionCheckStatus, RevisionStatus
from code_mule.domain.models import ProjectRevision
from code_mule.state.models import ProjectState


def latest_revision(state: ProjectState) -> ProjectRevision | None:
    return state.revisions[-1] if state.revisions else None


def completed_revision(state: ProjectState) -> ProjectRevision | None:
    completed = tuple(
        revision
        for revision in state.revisions
        if revision.lifecycle_status is RevisionStatus.COMPLETED
    )
    return completed[-1] if completed else None


def next_revision_number(state: ProjectState) -> int:
    latest = latest_revision(state)
    return 1 if latest is None else latest.revision_number + 1


def begin_revision(
    state: ProjectState,
    *,
    revision_number: int,
    plan_id: str,
    plan_version: int,
    change_request_id: str | None,
    base_revision: int | None,
    started_at: datetime,
    baseline_head: str | None,
) -> ProjectState:
    if any(item.revision_number == revision_number for item in state.revisions):
        raise ValueError(
            f"revision already exists: {revision_number}"
        )
    revision = ProjectRevision(
        revision_number=revision_number,
        started_at=started_at,
        lifecycle_status=RevisionStatus.IN_PROGRESS,
        plan_id=plan_id,
        plan_version=plan_version,
        base_revision=base_revision,
        change_request_id=change_request_id,
        baseline_head=baseline_head,
    )
    return replace(state, revisions=state.revisions + (revision,))


def complete_revision(
    state: ProjectState,
    *,
    revision_number: int,
    plan_id: str,
    plan_version: int,
    completion_head: str,
    verification_result_id: str,
    completed_at: datetime,
) -> ProjectState:
    """Persist immutable completion evidence on the named revision."""

    existing = tuple(
        item for item in state.revisions
        if item.revision_number == revision_number
    )
    if len(existing) != 1:
        raise ValueError(
            f"revision to complete must exist exactly once: {revision_number}"
        )
    revision = replace(
        existing[0],
        lifecycle_status=RevisionStatus.COMPLETED,
        plan_id=plan_id,
        plan_version=plan_version,
        completed_at=completed_at,
        completion_head=completion_head,
        verification_status=RevisionCheckStatus.PASS,
        final_review_status=RevisionCheckStatus.PASS,
        verification_result_id=verification_result_id,
    )
    return replace(
        state,
        revisions=tuple(
            revision if item.revision_number == revision_number else item
            for item in state.revisions
        ),
    )
