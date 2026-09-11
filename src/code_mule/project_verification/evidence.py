"""Bounded persisted facts for a final-review Human Gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from code_mule.domain.enums import HumanActionCategory, HumanActionStatus
from code_mule.domain.models import HumanAction, ProjectRevision

from .contracts import (
    FinalReviewDecision,
    ProjectVerificationCheck,
    ProjectVerificationResult,
)

if TYPE_CHECKING:
    from code_mule.state.models import ProjectState


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


@dataclass(frozen=True)
class FinalReviewHumanJudgmentEvidence:
    result: ProjectVerificationResult
    revision: ProjectRevision | None
    summary: str
    issues: tuple[str, ...]
    checks: tuple[ProjectVerificationCheck, ...]
    completion_head_candidate: str


def final_review_human_judgment_evidence(
    state: ProjectState, action: HumanAction
) -> FinalReviewHumanJudgmentEvidence | None:
    """Recognize new and schema-v14 legacy final-review decision gates."""

    if action.status is not HumanActionStatus.PENDING or action.task_id is not None:
        return None
    if action.category not in {
        HumanActionCategory.FINAL_REVIEW_DECISION,
        HumanActionCategory.SUPERVISOR_FAILURE,
    }:
        return None
    source = tuple(
        event
        for event in state.events
        if event.timestamp == action.created_at
        and event.event_type
        in {
            "project.final_review_human_judgment",
            "project.final_review_failed",
        }
    )
    if len(source) != 1:
        return None
    result_id = source[0].metadata.get("result_id")
    results = tuple(
        result
        for result in state.project_verification_results
        if result.id == result_id
        and result.final_review_decision is FinalReviewDecision.HUMAN_REQUIRED
    )
    if len(results) != 1:
        return None
    result = results[0]
    issues = tuple(
        _bounded(source[0].metadata[key], 300)
        for key in sorted(source[0].metadata)
        if key.startswith("issue_") and key[6:].isdigit()
    )[:5]
    revisions = tuple(
        revision for revision in state.revisions if revision.plan_id == result.plan_id
    )
    return FinalReviewHumanJudgmentEvidence(
        result=result,
        revision=revisions[-1] if revisions else None,
        summary=_bounded(result.final_review_summary or "Final review needs Boss judgment.", 600),
        issues=issues,
        checks=result.checks,
        completion_head_candidate=_bounded(result.verified_head, 128),
    )


__all__ = [
    "FinalReviewHumanJudgmentEvidence",
    "final_review_human_judgment_evidence",
]
