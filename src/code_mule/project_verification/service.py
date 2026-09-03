"""Deterministic final verification and project completion boundary."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import subprocess
from typing import Protocol

from code_mule.domain import HumanActionCategory, PlanStatus, ProjectEvent, ProjectStatus, TaskStatus
from code_mule.domain.state_machine import validate_transition
from code_mule.human import request_human_action
from code_mule.progress import (
    ProgressEvent,
    ProgressEventType,
    ProgressSink,
    resilient_progress_sink,
)
from code_mule.state.models import ProjectState
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
        if state.project.status is ProjectStatus.DONE:
            return state
        state = self._event_save(state, "project.verification_started", {})
        try:
            result = self._verification.run(state)
        except Exception as error:
            return self._human(
                self._store.load(),
                HumanActionCategory.RECOVERY_UNCERTAIN,
                "Project verification could not start safely",
                "Inspect verification configuration and repository state",
                {"error_type": type(error).__name__},
            )
        state = self._store.load()
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
            return self._human(
                self._store.load(),
                HumanActionCategory.SUPERVISOR_FAILURE,
                "Final Supervisor review failed",
                "Inspect the typed Supervisor failure and choose an explicit resolution",
                supervisor_failure_metadata(error),
                source_event_type="project.final_review_failed",
            )
        result = replace(
            result,
            final_review_decision=review.decision,
            final_review_summary=review.rationale,
        )
        latest = self._store.load()
        results = tuple(
            result if item.id == result.id else item
            for item in latest.project_verification_results
        )
        latest = replace(latest, project_verification_results=results)
        state = self._event_save(
            latest,
            "project.final_review_completed",
            {"result_id": result.id, "decision": review.decision.value},
        )
        if review.decision is FinalReviewDecision.HUMAN_REQUIRED:
            return self._human(
                state,
                HumanActionCategory.SUPERVISOR_FAILURE,
                "Final Supervisor review requires human judgment",
                "Inspect final delivery evidence and choose a Boss-directed change",
                {"result_id": result.id},
                source_event_type="project.final_review_failed",
            )
        return self._complete(self._store.load(), result)

    def _complete(self, state: ProjectState, result: ProjectVerificationResult) -> ProjectState:
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
        events = (
            self._event(completed, "plan.completed", plan.id, operation_time, {}),
            self._event(completed, "project.completed", state.project.id, operation_time, {"verification_result_id": result.id}),
        )
        completed = replace(completed, events=completed.events + events)
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
    ) -> ProjectState:
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
