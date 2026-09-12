"""Deterministic final verification and project completion boundary."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
from typing import Protocol

from code_mule.domain import (
    HumanActionCategory,
    PlanStatus,
    ProjectEvent,
    ProjectStatus,
    RevisionCheckStatus,
    RevisionStatus,
    TaskStatus,
)
from code_mule.domain.state_machine import validate_transition
from code_mule.human import request_human_action
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.state.models import ProjectState
from code_mule.recovery import (
    BoundaryRecoverability,
    ExecutionPhase,
    ExecutionStopReason,
    SafePointKind,
    WorkerTerminalState,
)
from code_mule.recovery.state import with_safe_point, with_stop_boundary
from code_mule.revision import (
    begin_revision,
    complete_revision,
    latest_revision,
)
from code_mule.runtime_handoff import InvalidDeliveryManifest
from code_mule.runtime_handoff.validation import (
    load_and_validate_manifest,
    validate_manifest,
)
from code_mule.supervisor import (
    FinalReviewRequest,
    FinalReviewResult,
    supervisor_failure_metadata,
)

from .contracts import (
    FinalReviewDecision,
    InvalidProjectVerificationState,
    ProjectVerificationCategory,
    ProjectVerificationCheck,
    ProjectVerificationCommand,
    ProjectVerificationResult,
    ProjectVerificationSpec,
    ProjectVerificationStatus,
)


_SENSITIVE_REVIEW_VALUE = re.compile(
    r"(?i)(?:(?:api[_ -]?key|token|password|authorization)\s*[:=]\s*|bearer\s+|sk-)[^\s,;]+"
)


def _safe_review_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    redacted = _SENSITIVE_REVIEW_VALUE.sub("[REDACTED]", normalized)
    return redacted if len(redacted) <= limit else redacted[: limit - 1] + "…"


class ProjectStateStore(Protocol):
    def load(self) -> ProjectState: ...
    def save(self, state: ProjectState) -> None: ...


class FinalReviewService(Protocol):
    def final_review(self, request: FinalReviewRequest) -> FinalReviewResult: ...


class VerificationProcessRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        environment: Mapping[str, str],
    ) -> tuple[int, bool]: ...


def run_verification_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[int, bool]:
    try:
        completed = subprocess.run(
            tuple(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 0, True
    except OSError:
        return 127, False
    return completed.returncode, False


class ProjectVerificationService:
    """Run only persisted deterministic commands plus mandatory Git checks."""

    _CATEGORIES = (
        ProjectVerificationCategory.TEST,
        ProjectVerificationCategory.LINT,
        ProjectVerificationCategory.TYPECHECK,
        ProjectVerificationCategory.BUILD,
    )

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        result_id_factory: Callable[[], str],
        runner: VerificationProcessRunner = run_verification_process,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._clock = clock
        self._result_id_factory = result_id_factory
        self._runner = runner
        source = os.environ if environment is None else environment
        self._environment = {
            key: value
            for key, value in source.items()
            if key in {"PATH", "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH"}
        }

    def run(self, state: ProjectState) -> ProjectVerificationResult:
        plan_id, workspace, expected_head = self._validate(state)
        started = self._clock()
        spec = state.project_verification_spec or ProjectVerificationSpec(
            state.project.id, ()
        )
        checks: list[ProjectVerificationCheck] = []
        configured = {item.category for item in spec.commands}
        for command in spec.commands:
            checks.append(self._run_command(command, workspace))
        for category in self._CATEGORIES:
            if category not in configured:
                checks.append(
                    ProjectVerificationCheck(
                        category.value,
                        category,
                        (),
                        ProjectVerificationStatus.SKIPPED,
                        None,
                        "No deterministic project check is configured.",
                        False,
                    )
                )
        checks.append(self._manifest_check(state, workspace))
        verified_head, git_check = self._git_check(workspace, expected_head)
        checks.append(git_check)
        return ProjectVerificationResult(
            id=self._result_id_factory(),
            project_id=state.project.id,
            plan_id=plan_id,
            expected_head=expected_head,
            verified_head=verified_head,
            checks=tuple(checks),
            started_at=started,
            completed_at=self._clock(),
        )

    @staticmethod
    def _manifest_check(
        state: ProjectState, workspace: Path
    ) -> ProjectVerificationCheck:
        if not state.delivery_manifest_required:
            return ProjectVerificationCheck(
                "Delivery manifest",
                ProjectVerificationCategory.DELIVERY_MANIFEST,
                (),
                ProjectVerificationStatus.SKIPPED,
                None,
                "Historical project does not require a synthesized manifest.",
                False,
            )
        plan = next(
            item for item in state.plans if item.id == state.project.active_plan_id
        )
        revision = latest_revision(state)
        revision_number = 1 if revision is None else revision.revision_number
        matching = tuple(
            item for item in state.delivery_manifests
            if item.revision_number == revision_number
            and item.plan_version == plan.version
        )
        valid = len(matching) == 1
        if valid:
            try:
                validate_manifest(matching[0], workspace)
            except InvalidDeliveryManifest:
                valid = False
        return ProjectVerificationCheck(
            "Delivery manifest",
            ProjectVerificationCategory.DELIVERY_MANIFEST,
            (),
            ProjectVerificationStatus.PASS if valid else ProjectVerificationStatus.FAIL,
            0 if valid else 1,
            (
                "Revision-scoped delivery manifest is verified."
                if valid
                else "Current revision has no valid delivery manifest."
            ),
            True,
        )

    def _run_command(
        self, command: ProjectVerificationCommand, workspace: Path
    ) -> ProjectVerificationCheck:
        if command.network_allowed:
            return ProjectVerificationCheck(
                command.name,
                command.category,
                command.command,
                ProjectVerificationStatus.FAIL,
                126,
                "Network-enabled verification requires explicit human approval.",
                command.required,
            )
        exit_code, timed_out = self._runner(
            command.command,
            cwd=workspace,
            timeout=command.timeout_seconds,
            environment=self._environment,
        )
        if timed_out:
            status = ProjectVerificationStatus.TIMEOUT
            exit_code_value = None
            summary = "Verification timed out without retaining command output."
        elif exit_code == 0:
            status = ProjectVerificationStatus.PASS
            exit_code_value = 0
            summary = "Verification command passed."
        else:
            status = ProjectVerificationStatus.FAIL
            exit_code_value = exit_code
            summary = "Verification command failed; output was not persisted."
        return ProjectVerificationCheck(
            command.name,
            command.category,
            command.command,
            status,
            exit_code_value,
            summary,
            command.required,
        )

    def _git_check(
        self, workspace: Path, expected_head: str
    ) -> tuple[str, ProjectVerificationCheck]:
        command = ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all")
        try:
            root = subprocess.run(
                ("git", "rev-parse", "--show-toplevel"), cwd=workspace,
                text=True, capture_output=True, check=True, env=self._environment,
            ).stdout.strip()
            head = subprocess.run(
                ("git", "rev-parse", "HEAD"), cwd=workspace,
                text=True, capture_output=True, check=True, env=self._environment,
            ).stdout.strip()
            status = subprocess.run(
                command, cwd=workspace, text=True, capture_output=True, check=True,
                env=self._environment,
            ).stdout
            valid = Path(root).resolve() == workspace.resolve() and head == expected_head and status == ""
        except (OSError, subprocess.CalledProcessError):
            head = "unavailable"
            valid = False
        return head, ProjectVerificationCheck(
            "Git clean",
            ProjectVerificationCategory.GIT_CLEAN,
            command,
            ProjectVerificationStatus.PASS if valid else ProjectVerificationStatus.FAIL,
            0 if valid else 1,
            (
                "Repository is clean at the final Task delivery HEAD."
                if valid
                else "Repository root, HEAD, staged, or worktree state is not final."
            ),
            True,
        )

    @staticmethod
    def _validate(state: ProjectState) -> tuple[str, Path, str]:
        if state.project.status is not ProjectStatus.RUNNING:
            raise InvalidProjectVerificationState("project must be RUNNING")
        if state.project.current_task_id is not None:
            raise InvalidProjectVerificationState("final verification requires a Task boundary")
        plan = next(
            (item for item in state.plans if item.id == state.project.active_plan_id),
            None,
        )
        if plan is None or plan.status is not PlanStatus.ACTIVE:
            raise InvalidProjectVerificationState("an active Plan is required")
        milestone_ids = set(plan.milestone_ids)
        task_ids = {
            task_id
            for milestone in state.milestones
            if milestone.id in milestone_ids
            for task_id in milestone.task_ids
        }
        tasks = tuple(task for task in state.tasks if task.id in task_ids)
        if not tasks or any(task.status is not TaskStatus.COMPLETED for task in tasks):
            raise InvalidProjectVerificationState("all active Plan Tasks must be completed")
        if state.project_verification_spec is not None and state.project_verification_spec.project_id != state.project.id:
            raise InvalidProjectVerificationState("verification spec project mismatch")
        if state.project.workspace is None:
            raise InvalidProjectVerificationState("project workspace is unavailable")
        workspace = Path(state.project.workspace)
        if not workspace.is_absolute() or not workspace.is_dir():
            raise InvalidProjectVerificationState("project workspace is invalid")
        commits = tuple(item for item in state.git_commit_results if item.task_id in task_ids)
        if not commits:
            raise InvalidProjectVerificationState("final Task delivery commit is unavailable")
        return plan.id, workspace, commits[-1].commit_sha


class ProjectFinalizationService:
    """Persist verification, request final reasoning, then atomically complete."""

    def __init__(
        self,
        *,
        store: ProjectStateStore,
        verification: ProjectVerificationService,
        supervisor: FinalReviewService,
        clock: Callable[[], datetime],
        event_id_factory: Callable[[], str],
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self._store = store
        self._verification = verification
        self._supervisor = supervisor
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._progress = resilient_progress_sink(progress_sink)

    @property
    def progress_errors(self) -> tuple[BaseException, ...]:
        return self._progress.errors

    def finalize(self, state: ProjectState) -> ProjectState:
        state = self._store.load()
        if state.project.status is ProjectStatus.CANCELLED:
            return state
        if state.project.status is ProjectStatus.DONE:
            return state
        try:
            state = self._ensure_delivery_manifest(state)
        except InvalidDeliveryManifest as error:
            return self._human(
                self._store.load(),
                HumanActionCategory.RECOVERY_UNCERTAIN,
                "Delivery manifest is missing or invalid",
                "Correct the revision delivery manifest before final verification",
                {"error_type": type(error).__name__},
            )
        state = self._event_save(state, "project.verification_started", {})
        try:
            result = self._verification.run(state)
        except Exception as error:
            latest = self._store.load()
            if latest.project.status is ProjectStatus.CANCELLED:
                return latest
            return self._human(
                latest,
                HumanActionCategory.RECOVERY_UNCERTAIN,
                "Project verification could not start safely",
                "Inspect verification configuration and repository state",
                {"error_type": type(error).__name__},
            )
        state = self._store.load()
        if state.project.status is ProjectStatus.CANCELLED:
            return state
        state = replace(
            state,
            project_verification_results=state.project_verification_results + (result,),
        )
        state = self._event_save(
            state,
            "project.verification_completed",
            {"result_id": result.id, "passed": str(result.passed).lower()},
        )

        if not result.passed:
            return self._human(
                self._store.load(),
                HumanActionCategory.RECOVERY_UNCERTAIN,
                "Project-level verification failed",
                "Inspect the failed check and choose an explicit resolution",
                {"result_id": result.id},
            )
        state = self._event_save(
            self._store.load(),
            "project.final_review_started",
            {"result_id": result.id},
        )
        try:
            review = self._supervisor.final_review(FinalReviewRequest(state, result))
        except Exception as error:
            latest = self._store.load()
            if latest.project.status is ProjectStatus.CANCELLED:
                return latest
            return self._human(
                latest,
                HumanActionCategory.SUPERVISOR_FAILURE,
                "Final Supervisor review failed",
                "Inspect the typed Supervisor failure and choose an explicit resolution",
                supervisor_failure_metadata(error),
                source_event_type="project.final_review_failed",
            )
        result = replace(
            result,
            final_review_decision=review.decision,
            final_review_summary=_safe_review_text(review.rationale, 600),
        )
        latest = self._store.load()
        if latest.project.status is ProjectStatus.CANCELLED:
            return latest
        results = tuple(
            result if item.id == result.id else item
            for item in latest.project_verification_results
        )
        latest = replace(latest, project_verification_results=results)
        latest = self._record_revision_review(latest, result, review.decision)
        issue_metadata = {
            f"issue_{index}": _safe_review_text(issue, 300)
            for index, issue in enumerate(review.issues[:5], start=1)
            if " ".join(issue.split())
        }
        state = self._event_save(
            latest,
            "project.final_review_completed",
            {
                "result_id": result.id,
                "decision": review.decision.value,
                "issue_count": str(len(issue_metadata)),
                **issue_metadata,
            },
        )
        if review.decision is FinalReviewDecision.HUMAN_REQUIRED:
            return self._human(
                state,
                HumanActionCategory.FINAL_REVIEW_DECISION,
                "Final Supervisor review requires human judgment",
                "Inspect the bounded finding, then request a change or fail the project",
                {"result_id": result.id, **issue_metadata},
                source_event_type="project.final_review_human_judgment",
                phase=ExecutionPhase.FINALIZATION,
            )
        return self._complete(self._store.load(), result)

    def _ensure_delivery_manifest(self, state: ProjectState) -> ProjectState:
        if not state.delivery_manifest_required:
            return state
        plan = next(
            (item for item in state.plans if item.id == state.project.active_plan_id),
            None,
        )
        if plan is None or state.project.workspace is None:
            raise InvalidDeliveryManifest("active revision context is unavailable")
        revision = latest_revision(state)
        revision_number = 1 if revision is None else revision.revision_number
        matching = tuple(
            item for item in state.delivery_manifests
            if item.revision_number == revision_number
            and item.plan_version == plan.version
        )
        if len(matching) > 1:
            raise InvalidDeliveryManifest("delivery manifest identity is ambiguous")
        if matching:
            validate_manifest(matching[0], Path(state.project.workspace))
            return state
        manifest = load_and_validate_manifest(
            Path(state.project.workspace),
            project_id=state.project.id,
            revision_number=revision_number,
            plan_version=plan.version,
            generated_at=self._clock(),
        )
        now = self._clock()
        updated = replace(
            state,
            project=replace(state.project, updated_at=now),
            delivery_manifests=state.delivery_manifests + (manifest,),
            events=state.events + (
                self._event(
                    state,
                    "delivery.manifest_verified",
                    manifest.id,
                    now,
                    {
                        "revision_number": str(revision_number),
                        "plan_version": str(plan.version),
                        "deliverable_type": manifest.deliverable_type.value,
                    },
                ),
            ),
        )
        self._store.save(updated)
        return updated

    @staticmethod
    def _record_revision_review(
        state: ProjectState,
        result: ProjectVerificationResult,
        decision: FinalReviewDecision,
    ) -> ProjectState:
        status = (
            RevisionCheckStatus.PASS
            if decision is FinalReviewDecision.APPROVE
            else RevisionCheckStatus.UNKNOWN
        )
        return replace(
            state,
            revisions=tuple(
                replace(
                    revision,
                    verification_status=RevisionCheckStatus.PASS,
                    final_review_status=status,
                    verification_result_id=result.id,
                )
                if revision.plan_id == result.plan_id
                and revision.lifecycle_status is RevisionStatus.IN_PROGRESS
                else revision
                for revision in state.revisions
            ),
        )

    def _complete(self, state: ProjectState, result: ProjectVerificationResult) -> ProjectState:
        state = self._store.load()
        if state.project.status is ProjectStatus.CANCELLED:
            return state
        plan = next(item for item in state.plans if item.id == state.project.active_plan_id)
        operation_time = self._clock()
        validate_transition(state.project.status, ProjectStatus.DONE)
        milestone_ids = set(plan.milestone_ids)
        completed = replace(
            state,
            project=replace(state.project, status=ProjectStatus.DONE, current_task_id=None, updated_at=operation_time),
            plans=tuple(replace(item, status=PlanStatus.COMPLETED) if item.id == plan.id else item for item in state.plans),
            milestones=tuple(replace(item, status="completed") if item.id in milestone_ids else item for item in state.milestones),
        )
        active = latest_revision(completed)
        revision_number = (
            1 if active is None else active.revision_number
        )
        if active is None:
            completed = begin_revision(
                completed,
                revision_number=1,
                plan_id=plan.id,
                plan_version=plan.version,
                change_request_id=None,
                base_revision=None,
                started_at=operation_time,
                baseline_head=None,
            )
            active = latest_revision(completed)
        if active is not None and active.lifecycle_status is not RevisionStatus.COMPLETED:
            completed = complete_revision(
                completed,
                revision_number=revision_number,
                plan_id=plan.id,
                plan_version=plan.version,
                completion_head=result.verified_head,
                verification_result_id=result.id,
                completed_at=operation_time,
            )
        events = (
            self._event(completed, "plan.completed", plan.id, operation_time, {}),
            self._event(completed, "project.completed", state.project.id, operation_time, {"verification_result_id": result.id}),
        )
        completed = replace(completed, events=completed.events + events)
        completed = with_safe_point(
            completed, SafePointKind.PROJECT_DONE, operation_time,
            head_sha=result.verified_head,
        )
        completed = with_stop_boundary(
            completed,
            reason=ExecutionStopReason.EXECUTION_COMPLETED,
            phase=ExecutionPhase.FINALIZATION,
            safe_point=SafePointKind.PROJECT_DONE,
            recoverability=BoundaryRecoverability.TERMINAL,
            worker_started=False,
            worker_terminal_state=WorkerTerminalState.NOT_STARTED,
            report_persisted=False,
            recorded_at=operation_time,
            head_sha=result.verified_head,
        )
        self._store.save(completed)
        return completed

    def _human(
        self,
        state: ProjectState,
        category: HumanActionCategory,
        summary: str,
        requested_action: str,
        metadata: dict[str, str],
        *,
        source_event_type: str = "project.verification_failed",
        phase: ExecutionPhase | None = None,
    ) -> ProjectState:
        state = self._store.load()
        if state.project.status is ProjectStatus.CANCELLED:
            return state
        if state.project.status is ProjectStatus.HUMAN_REQUIRED:
            return state
        updated = request_human_action(
            state,
            category=category,
            summary=summary,
            requested_action=requested_action,
            risk="Project completion cannot be trusted until this action is resolved",
            task_id=None,
            operation_time=self._clock(),
            action_id=f"action-{self._event_id_factory()}",
            event_id_factory=self._event_id_factory,
            source_event_types=(source_event_type,),
            source_metadata=metadata,
            phase=phase,
        )
        self._store.save(updated)
        return updated

    def _event_save(self, state: ProjectState, event_type: str, metadata: dict[str, str]) -> ProjectState:
        now = self._clock()
        updated = replace(
            state,
            project=replace(state.project, updated_at=now),
            events=state.events + (self._event(state, event_type, state.project.id, now, metadata),),
        )
        if event_type == "project.verification_started":
            updated = with_safe_point(
                updated, SafePointKind.PROJECT_FINALIZING, now
            )
        self._store.save(updated)
        progress_types = {
            "project.verification_started": (
                ProgressEventType.PROJECT_VERIFICATION_STARTED,
                "Final project verification started",
            ),
            "project.verification_completed": (
                ProgressEventType.PROJECT_VERIFICATION_COMPLETED,
                "Final project verification completed",
            ),
            "project.final_review_started": (
                ProgressEventType.PROJECT_FINAL_REVIEW_STARTED,
                "Supervisor final review started",
            ),
            "project.final_review_completed": (
                ProgressEventType.PROJECT_FINAL_REVIEW_COMPLETED,
                "Supervisor final review completed",
            ),
        }
        progress = progress_types.get(event_type)
        if progress is not None:
            self._progress.emit(
                ProgressEvent(
                    progress[0],
                    now,
                    state.project.id,
                    None,
                    None,
                    progress[1],
                    metadata,
                )
            )
        return updated

    def _event(self, state: ProjectState, event_type: str, entity_id: str, timestamp: datetime, metadata: dict[str, str]) -> ProjectEvent:
        return ProjectEvent(self._event_id_factory(), state.project.id, event_type, entity_id, timestamp, metadata)


__all__ = [
    "FinalReviewService",
    "ProjectFinalizationService",
    "ProjectStateStore",
    "ProjectVerificationService",
    "VerificationProcessRunner",
    "run_verification_process",
]
