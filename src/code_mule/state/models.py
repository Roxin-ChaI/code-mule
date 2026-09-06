"""Snapshot model for durable Code Mule project state."""

from dataclasses import dataclass

from code_mule.domain.models import (
    ChangeRequest,
    Decision,
    ExecutionReport,
    ImpactAnalysis,
    HumanAction,
    HumanResolution,
    Milestone,
    Plan,
    Project,
    ProjectEvent,
    QualityStatus,
    Requirement,
    Task,
)
from code_mule.execution.contracts import ExecutionLease
from code_mule.git_delivery.contracts import GitBaseline, GitChangeSet, GitCommitResult
from code_mule.project_verification.contracts import (
    ProjectVerificationResult,
    ProjectVerificationSpec,
)
from code_mule.recovery.contracts import ExecutionAttempt, ExecutionStopBoundary, SafePoint


@dataclass
class ProjectState:
    project: Project
    requirements: tuple[Requirement, ...]
    plans: tuple[Plan, ...]
    milestones: tuple[Milestone, ...]
    tasks: tuple[Task, ...]
    change_requests: tuple[ChangeRequest, ...]
    impact_analyses: tuple[ImpactAnalysis, ...]
    decisions: tuple[Decision, ...]
    execution_reports: tuple[ExecutionReport, ...]
    quality_status: QualityStatus | None
    events: tuple[ProjectEvent, ...]
    human_actions: tuple[HumanAction, ...] = ()
    human_resolutions: tuple[HumanResolution, ...] = ()
    execution_leases: tuple[ExecutionLease, ...] = ()
    git_baselines: tuple[GitBaseline, ...] = ()
    git_change_sets: tuple[GitChangeSet, ...] = ()
    git_commit_results: tuple[GitCommitResult, ...] = ()
    project_verification_spec: ProjectVerificationSpec | None = None
    project_verification_results: tuple[ProjectVerificationResult, ...] = ()
    latest_execution_stop: ExecutionStopBoundary | None = None
    latest_safe_point: SafePoint | None = None
    execution_attempts: tuple[ExecutionAttempt, ...] = ()


__all__ = ["ProjectState"]
