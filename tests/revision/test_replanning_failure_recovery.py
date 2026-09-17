from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli import CliExitCode, main
from code_mule.cli.composition import ProductionCliComposition, RuntimeComposition
from code_mule.domain import (
    ChangeRequestStatus,
    HumanActionCategory,
    HumanActionStatus,
    HumanResolutionStrategy,
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import Plan
from code_mule.diagnosis import DiagnosisStage, ProjectDiagnosisService
from code_mule.git_delivery import GitCommitResult
from code_mule.human import (
    HumanResolutionService,
    allowed_resolution_strategies,
)
from code_mule.orchestrator import ChangeCommand, OrchestratorService
from code_mule.presentation import (
    render_change_requested,
    render_human_action,
    render_project_diagnosis,
)
from code_mule.progress import ConsoleProgressRenderer
from code_mule.project_verification.service import (
    ProjectFinalizationService,
    ProjectVerificationService,
)
from code_mule.project_verification import FinalReviewDecision
from code_mule.recovery import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionPhase,
    RecoveryMode,
)
from code_mule.recovery.service import RecoveryClassifier
from code_mule.replanning import (
    ChangeExecutionService,
    ChangeReplanningRequest,
    PostCompletionReplanningStage,
    ReplanFailureCode,
)
from code_mule.replanning.errors import (
    InvalidReplanningState,
    ReplanMaterializationError,
)
from code_mule.replanning.recovery import (
    post_completion_replanning_failure_evidence,
    post_completion_replanning_retry_safety,
)
from code_mule.replanning.reopen import PostCompletionReplanner
from code_mule.runtime import ProjectExecutionOutcome, ProjectExecutionStopReason
from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    deserialize_project_state,
    serialize_project_state,
)
from code_mule.state.store import JsonProjectStateStore
from code_mule.supervisor import FinalReviewResult
from code_mule.supervisor.parsing import parse_impact_analysis_response
from code_mule.supervisor.prompts import build_impact_analysis_prompt
from code_mule.supervisor.schemas import impact_analysis_response_schema
from code_mule.supervisor.service import SupervisorService
from code_mule.supervisor import ImpactAnalysisRequest

from revision.test_phase24 import (
    EventIds,
    MemoryStore,
    done_v1_state,
    proposal_v2,
    verification_evidence,
)


NOW = datetime(2026, 9, 11, tzinfo=UTC)


class Clock:
    def __init__(self, start=NOW):
        self.start = start
        self.calls = 0

    def __call__(self):
        value = self.start + timedelta(seconds=self.calls)
        self.calls += 1
        return value


class SequenceSupervisor:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.requests = []

    def analyze_change(self, request):
        self.requests.append(request)
        return self.proposals.pop(0)


class CapturingModelClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create_structured_response(self, **request):
        self.calls.append(request)
        return self.payload


class ApprovingFinalSupervisor:
    def final_review(self, request):
        return FinalReviewResult(
            FinalReviewDecision.APPROVE,
            "Revision evidence is complete.",
            (),
        )


class NoWorkerExecution:
    def __init__(self):
        self.calls = 0

    def run(self):
        self.calls += 1
        return ProjectExecutionOutcome(
            "project-1",
            0,
            0,
            (),
            ProjectStatus.RUNNING,
            False,
            False,
            ProjectExecutionStopReason.TASK_LIMIT_REACHED,
        )


class PostCompletionFailureFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.root, check=True)
        subprocess.run(
            ("git", "config", "user.name", "Revision Test"),
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ("git", "config", "user.email", "revision@example.invalid"),
            cwd=self.root,
            check=True,
        )
        (self.root / "README.md").write_text("# Revision one\n")
        subprocess.run(("git", "add", "README.md"), cwd=self.root, check=True)
        subprocess.run(
            ("git", "commit", "-qm", "revision one"),
            cwd=self.root,
            check=True,
        )
        self.head = self.git("rev-parse", "HEAD")
        verification = replace(
            verification_evidence(),
            expected_head=self.head,
            verified_head=self.head,
        )
        base = done_v1_state(verification=verification)
        base = replace(
            base,
            project=replace(base.project, workspace=str(self.root)),
            revisions=(
                replace(base.revisions[0], completion_head=self.head),
            ),
            latest_safe_point=replace(
                base.latest_safe_point, head_sha=self.head
            ),
        )
        store = MemoryStore(base)
        OrchestratorService(
            store,
            clock=Clock(NOW + timedelta(hours=2)),
            event_id_factory=EventIds(),
        ).change(ChangeCommand("project-1", "Persist counter", "boss", "CR-2"))
        self.requested = store.state

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.run(
            ("git", *arguments),
            cwd=self.root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    @staticmethod
    def invalid_proposal():
        return replace(
            proposal_v2(),
            milestone_ids_reused=("M1",),
        )

    @staticmethod
    def lineage_conflict_proposal():
        proposal = proposal_v2()
        replacement = proposal.tasks_to_add[0]
        return replace(
            proposal,
            tasks_to_add=(
                replace(
                    replacement,
                    derived_from_task_ids=(replacement.supersedes_task_id,),
                ),
            ),
        )

    @staticmethod
    def proposal_payload(proposal=None):
        selected = proposal or proposal_v2()
        return {
            "change_request_id": selected.change_request_id,
            "summary": selected.summary,
            "architecture_impact": selected.architecture_impact,
            "affected_components": list(selected.affected_components),
            "affected_requirement_ids": list(selected.affected_requirement_ids),
            "affected_task_ids": list(selected.affected_task_ids),
            "affected_completed_tasks": list(selected.affected_completed_tasks),
            "affected_in_progress_tasks": [],
            "affected_pending_tasks": [],
            "requirements_to_add": [],
            "requirements_to_update": [],
            "tasks_to_add": [
                {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "dependencies": list(task.dependencies),
                    "acceptance_criteria": list(task.acceptance_criteria),
                    "requirement_ids": list(task.requirement_ids),
                    "supersedes_task_id": task.supersedes_task_id,
                    "derived_from_task_ids": list(task.derived_from_task_ids),
                }
                for task in selected.tasks_to_add
            ],
            "tasks_to_reopen": [],
            "tasks_to_cancel": [],
            "milestone_ids_reused": list(selected.milestone_ids_reused),
            "milestones": [
                {
                    "id": milestone.id,
                    "title": milestone.title,
                    "task_ids": list(milestone.task_ids),
                }
                for milestone in selected.milestones
            ],
            "dependency_changes": [],
            "task_requirement_updates": [],
            "risks": list(selected.risks),
            "recommendation": selected.recommendation,
            "rationale": selected.rationale,
        }

    def fail(self, *, state=None, supervisor=None):
        store = MemoryStore(state or self.requested)
        selected = supervisor or SequenceSupervisor((self.invalid_proposal(),))
        service = PostCompletionReplanner(
            store=store,
            supervisor=selected,
            clock=Clock(NOW + timedelta(hours=3)),
            plan_id_factory=lambda: "PLAN-2",
            event_id_factory=EventIds(),
        )
        with self.assertRaises(ReplanMaterializationError):
            service.replan(ChangeReplanningRequest("project-1", "CR-2"))
        return store, selected


class ReplanningFailureEvidenceTests(PostCompletionFailureFixture):
    def test_untyped_internal_materialization_error_is_not_masked_as_gate(self):
        class BrokenReplanner(PostCompletionReplanner):
            def _materialize(self, *args, **kwargs):
                raise ValueError("internal invariant")

        store = MemoryStore(self.requested)
        service = BrokenReplanner(
            store=store,
            supervisor=SequenceSupervisor((proposal_v2(),)),
            clock=Clock(NOW + timedelta(hours=3)),
            plan_id_factory=lambda: "PLAN-2",
            event_id_factory=EventIds(),
        )

        with self.assertRaisesRegex(ValueError, "internal invariant"):
            service.replan(ChangeReplanningRequest("project-1", "CR-2"))

        self.assertIs(store.state.project.status, ProjectStatus.REPLANNING)
        self.assertEqual(store.state.human_actions, ())
        self.assertEqual(len(store.state.plans), 1)
        self.assertEqual(len(store.state.revisions), 1)

    def test_conflicting_lineage_is_typed_before_materialization(self):
        store, _ = self.fail(
            supervisor=SequenceSupervisor((self.lineage_conflict_proposal(),))
        )
        action = store.state.human_actions[-1]
        evidence = post_completion_replanning_failure_evidence(
            store.state, action
        )

        self.assertIsNotNone(evidence)
        self.assertEqual(
            evidence.failure_code,
            ReplanFailureCode.CONFLICTING_TASK_LINEAGE.value,
        )
        self.assertEqual(
            evidence.field_path,
            "tasks_to_add[*].derived_from_task_ids",
        )
        self.assertEqual(
            evidence.failure_stage,
            PostCompletionReplanningStage.PROPOSAL_VALIDATION.value,
        )
        self.assertEqual(len(store.state.plans), 1)
        self.assertEqual(len(store.state.revisions), 1)
        self.assertEqual(len(store.state.tasks), 3)
        rendered = "\n".join(
            render_human_action(action, state=store.state, verbose=True)
        )
        self.assertIn("Conflicting task lineage", rendered)
        self.assertIn("tasks_to_add[*].derived_from_task_ids", rendered)

    def test_post_completion_schema_and_prompt_advertise_only_fresh_graph(self):
        schema = impact_analysis_response_schema(post_completion=True)
        properties = schema["properties"]
        for field_name in (
            "affected_in_progress_tasks",
            "affected_pending_tasks",
            "tasks_to_reopen",
            "tasks_to_cancel",
            "milestone_ids_reused",
            "dependency_changes",
            "task_requirement_updates",
        ):
            self.assertEqual(properties[field_name]["maxItems"], 0)
        self.assertEqual(properties["tasks_to_add"]["minItems"], 1)
        self.assertEqual(properties["milestones"]["minItems"], 1)

        replanning = replace(
            self.requested,
            project=replace(
                self.requested.project, status=ProjectStatus.REPLANNING
            ),
            change_requests=(
                replace(
                    self.requested.change_requests[0],
                    status=ChangeRequestStatus.ANALYZING,
                ),
            ),
        )
        request = ImpactAnalysisRequest(
            replanning, replanning.change_requests[0]
        )
        payload = self.proposal_payload()
        client = CapturingModelClient(payload)

        result = SupervisorService(client).analyze_change(request)
        _, prompt = build_impact_analysis_prompt(request)

        self.assertEqual(result.tasks_to_add[0].id, "T4")
        self.assertEqual(
            client.calls[0]["schema"],
            impact_analysis_response_schema(post_completion=True),
        )
        self.assertIn("never repeat supersedes_task_id", prompt)
        self.assertIn("Historical reuse is semantic lineage", prompt)
        self.assertIn("Code Mule owns the persistent Plan identity", prompt)

    def test_parser_preserves_lineage_fields_without_reclassifying_history(self):
        payload = self.proposal_payload(self.lineage_conflict_proposal())

        parsed = parse_impact_analysis_response(payload)

        task = parsed.tasks_to_add[0]
        self.assertEqual(task.id, "T4")
        self.assertEqual(task.supersedes_task_id, "T3")
        self.assertEqual(task.derived_from_task_ids, ("T3",))
        self.assertEqual(parsed.milestone_ids_reused, ())
        self.assertEqual(parsed.milestones[0].task_ids, ("T4",))

    def test_sanitized_failure_is_typed_before_any_v2_materialization(self):
        original_plan = serialize_project_state(self.requested)["plans"][0]
        original_revision = serialize_project_state(self.requested)["revisions"][0]
        original_tasks = serialize_project_state(self.requested)["tasks"]

        store, supervisor = self.fail()
        state = store.state
        action = state.human_actions[-1]
        evidence = post_completion_replanning_failure_evidence(state, action)

        self.assertEqual(len(supervisor.requests), 1)
        self.assertIs(state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertIs(action.category, HumanActionCategory.SUPERVISOR_FAILURE)
        self.assertIs(action.status, HumanActionStatus.PENDING)
        self.assertIsNotNone(evidence)
        self.assertFalse(evidence.plan_materialized)
        self.assertFalse(evidence.revision_materialized)
        self.assertEqual(evidence.base_revision, 1)
        self.assertEqual(evidence.requested_revision, 2)
        self.assertEqual(evidence.target_plan_version, 2)
        self.assertEqual(
            evidence.failure_code,
            ReplanFailureCode.HISTORICAL_MILESTONE_REUSE.value,
        )
        self.assertEqual(
            evidence.failure_stage,
            PostCompletionReplanningStage.PROPOSAL_VALIDATION.value,
        )
        event = evidence.failure_event
        self.assertEqual(event.metadata["operation"], "post_completion_replanning")
        self.assertEqual(event.metadata["field_path"], "milestone_ids_reused")
        self.assertEqual(event.metadata["plan_materialized"], "false")
        self.assertIs(state.latest_execution_stop.phase, ExecutionPhase.REPLANNING)
        self.assertEqual(state.impact_analyses, ())
        self.assertEqual(len(state.plans), 1)
        self.assertEqual(len(state.revisions), 1)
        self.assertEqual(len(state.tasks), 3)
        self.assertEqual(serialize_project_state(state)["plans"][0], original_plan)
        self.assertEqual(
            serialize_project_state(state)["revisions"][0], original_revision
        )
        self.assertEqual(serialize_project_state(state)["tasks"], original_tasks)

    def test_retry_resolution_is_side_effect_free_and_restartable(self):
        store, supervisor = self.fail()
        state = store.state
        action = state.human_actions[-1]

        self.assertTrue(
            post_completion_replanning_retry_safety(state, action).safe
        )
        self.assertEqual(
            allowed_resolution_strategies(state, action),
            (
                HumanResolutionStrategy.RETRY_REPLANNING,
                HumanResolutionStrategy.FAIL_PROJECT,
                HumanResolutionStrategy.ACKNOWLEDGE,
            ),
        )
        before_plans = state.plans
        before_revisions = state.revisions
        before_tasks = state.tasks
        resolved = HumanResolutionService(
            store,
            clock=Clock(NOW + timedelta(hours=4)),
            event_id_factory=EventIds(),
            resolution_id_factory=lambda: "resolution-retry",
        ).resolve(action.id, HumanResolutionStrategy.RETRY_REPLANNING)

        self.assertEqual(len(supervisor.requests), 1)
        self.assertIs(resolved.project.status, ProjectStatus.CHANGE_REQUESTED)
        self.assertIs(
            resolved.change_requests[-1].status, ChangeRequestStatus.PENDING
        )
        self.assertEqual(resolved.change_requests[-1].id, "CR-2")
        self.assertEqual(resolved.plans, before_plans)
        self.assertEqual(resolved.revisions, before_revisions)
        self.assertEqual(resolved.tasks, before_tasks)
        self.assertIs(
            RecoveryClassifier().classify(resolved, validate_workspace=True).recovery_mode,
            RecoveryMode.FRESH_REPLANNING,
        )
        restored = deserialize_project_state(serialize_project_state(resolved))
        self.assertEqual(CURRENT_SCHEMA_VERSION, 16)
        self.assertEqual(restored, resolved)

    def test_recover_reuses_request_and_materializes_only_one_v2(self):
        store, _ = self.fail()
        action = store.state.human_actions[-1]
        HumanResolutionService(
            store,
            clock=Clock(NOW + timedelta(hours=4)),
            event_id_factory=EventIds(),
            resolution_id_factory=lambda: "resolution-retry",
        ).resolve(action.id, HumanResolutionStrategy.RETRY_REPLANNING)
        restarted = MemoryStore(
            deserialize_project_state(serialize_project_state(store.state))
        )
        supervisor = SequenceSupervisor((proposal_v2(),))
        service = PostCompletionReplanner(
            store=restarted,
            supervisor=supervisor,
            clock=Clock(NOW + timedelta(hours=5)),
            plan_id_factory=lambda: "PLAN-2",
            event_id_factory=EventIds(),
        )

        outcome = service.replan(ChangeReplanningRequest("project-1", "CR-2"))

        self.assertEqual(outcome.plan_version, 2)
        self.assertEqual(len(supervisor.requests), 1)
        self.assertEqual(len(restarted.state.plans), 2)
        self.assertEqual(len(restarted.state.revisions), 2)
        self.assertEqual(
            tuple(plan.change_request_id for plan in restarted.state.plans),
            (None, "CR-2"),
        )
        self.assertEqual(
            tuple(revision.revision_number for revision in restarted.state.revisions),
            (1, 2),
        )
        self.assertIs(restarted.state.plans[0].status, PlanStatus.COMPLETED)
        self.assertEqual(
            restarted.state.revisions[0].lifecycle_status.value, "completed"
        )
        with self.assertRaises(InvalidReplanningState):
            service.replan(ChangeReplanningRequest("project-1", "CR-2"))
        self.assertEqual(len(restarted.state.plans), 2)
        self.assertEqual(len(restarted.state.revisions), 2)

    def test_materialization_worker_and_workspace_uncertainty_omit_retry(self):
        store, _ = self.fail()
        action = store.state.human_actions[-1]
        target = Plan(
            "PLAN-PARTIAL",
            "project-1",
            2,
            PlanStatus.ACTIVE,
            ("REQ-1",),
            (),
            NOW,
            base_plan_id="PLAN-1",
            base_plan_version=1,
            change_request_id="CR-2",
            revision_number=2,
        )
        uncertain = replace(store.state, plans=store.state.plans + (target,))
        safety = post_completion_replanning_retry_safety(
            uncertain, action, validate_workspace=False
        )
        self.assertFalse(safety.safe)
        self.assertNotIn(
            HumanResolutionStrategy.RETRY_REPLANNING,
            allowed_resolution_strategies(uncertain, action),
        )

        target_revision = replace(
            store.state.revisions[0],
            revision_number=2,
            base_revision=1,
            change_request_id="CR-2",
        )
        with_revision = replace(
            store.state,
            revisions=store.state.revisions + (target_revision,),
        )
        self.assertFalse(
            post_completion_replanning_retry_safety(
                with_revision, action, validate_workspace=False
            ).safe
        )

        attempt = ExecutionAttempt(
            "T1",
            2,
            ExecutionAttemptStatus.PREPARED,
            store.state.change_requests[-1].created_at + timedelta(seconds=1),
            baseline_head=self.head,
        )
        with_worker = replace(
            store.state,
            execution_attempts=store.state.execution_attempts + (attempt,),
        )
        self.assertFalse(
            post_completion_replanning_retry_safety(
                with_worker, action, validate_workspace=False
            ).safe
        )

        (self.root / "dirty.txt").write_text("dirty\n")
        self.assertFalse(
            post_completion_replanning_retry_safety(store.state, action).safe
        )

    def test_post_completion_prompt_matches_strict_materializer_contract(self):
        replanning = replace(
            self.requested,
            project=replace(
                self.requested.project, status=ProjectStatus.REPLANNING
            ),
            change_requests=(
                replace(
                    self.requested.change_requests[0],
                    status=ChangeRequestStatus.ANALYZING,
                ),
            ),
        )
        _, prompt = build_impact_analysis_prompt(
            ImpactAnalysisRequest(replanning, replanning.change_requests[0])
        )

        self.assertIn("post-completion replanning", prompt)
        self.assertIn("milestone_ids_reused", prompt)
        self.assertIn("must all be empty arrays", prompt)
        self.assertIn("fresh executable Task", prompt)
        self.assertNotIn("Correct reuse: reference M1", prompt)

    def test_done_and_running_change_wording_are_distinct(self):
        completed = "\n".join(
            render_change_requested(self.requested, "Persist counter")
        )
        self.assertIn("Completed revision will remain unchanged", completed)
        self.assertNotIn("Current task will finish", completed)

        running = replace(
            self.requested,
            revisions=(
                replace(
                    self.requested.revisions[0],
                    lifecycle_status=__import__(
                        "code_mule.domain", fromlist=["RevisionStatus"]
                    ).RevisionStatus.IN_PROGRESS,
                    completed_at=None,
                ),
            ),
        )
        active = "\n".join(render_change_requested(running, "Change now"))
        self.assertIn("Current task will finish safely", active)

    def test_inspect_contains_bounded_failure_and_replanning_facts(self):
        store, _ = self.fail()
        action = store.state.human_actions[-1]
        text = "\n".join(
            render_human_action(action, state=store.state, verbose=True)
        )
        self.assertIn("POST-COMPLETION REPLANNING FAILURE", text)
        self.assertIn("Stage        Proposal validation", text)
        self.assertIn("Base         Revision 1 · Plan v1", text)
        self.assertIn("Requested    Revision 2 · Plan v2", text)
        self.assertIn("Plan created No", text)
        self.assertIn("Revision created No", text)
        self.assertIn("Historical milestone reuse", text)
        self.assertIn("retry_replanning", text)
        self.assertNotIn("secret", text.lower())

    def test_diagnosis_identifies_replanning_boundary_and_target_version(self):
        store, _ = self.fail()

        diagnosis = ProjectDiagnosisService().diagnose(store.state)
        text = "\n".join(render_project_diagnosis(diagnosis, verbose=True))

        self.assertIs(diagnosis.blocker_stage, DiagnosisStage.REPLANNING)
        self.assertEqual(diagnosis.base_revision, 1)
        self.assertEqual(diagnosis.requested_revision, 2)
        self.assertEqual(diagnosis.target_plan_version, 2)
        self.assertFalse(diagnosis.plan_materialized)
        self.assertFalse(diagnosis.requested_revision_materialized)
        self.assertEqual(
            diagnosis.failure_code,
            ReplanFailureCode.HISTORICAL_MILESTONE_REUSE.value,
        )
        self.assertIn("Stage          Replanning", text)
        self.assertIn("Plan created  No", text)
        self.assertIn("Revision created No", text)


class ReplanningFailureCliTests(PostCompletionFailureFixture):
    def test_expected_gate_is_action_required_and_recover_uses_same_change(self):
        state_file = Path(self.temporary.name) / "project-state.json"
        JsonProjectStateStore(state_file).save(self.requested)
        supervisor = SequenceSupervisor(
            (self.lineage_conflict_proposal(), proposal_v2())
        )
        execution = NoWorkerExecution()
        stdout = StringIO()
        stderr = StringIO()

        def runtime_factory(state):
            store = JsonProjectStateStore(state_file)
            replanner = PostCompletionReplanner(
                store=store,
                supervisor=supervisor,
                clock=Clock(NOW + timedelta(hours=3 + len(supervisor.requests))),
                plan_id_factory=lambda: "PLAN-2",
                event_id_factory=EventIds(),
            )
            return RuntimeComposition(
                supervisor=supervisor,
                worker_service=object(),
                planning=object(),
                execution=execution,
                change_execution=ChangeExecutionService(
                    replanning_service=replanner,
                    execution_service=execution,
                ),
                renderer=ConsoleProgressRenderer(stderr),
            )

        composition = ProductionCliComposition(
            state_file,
            environment={},
            stdout=stdout,
            stderr=stderr,
            runtime_factory=runtime_factory,
        )
        code = main(
            ["change", "--apply"],
            composition_factory=lambda *_: composition,
            environment={},
            stdout=stdout,
            stderr=stderr,
        )
        failed = JsonProjectStateStore(state_file).load()

        self.assertEqual(code, CliExitCode.HUMAN_ACTION_REQUIRED)
        self.assertIn("ACTION REQUIRED", stdout.getvalue())
        self.assertNotIn("UNEXPECTED ERROR", stdout.getvalue() + stderr.getvalue())
        self.assertIs(failed.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(execution.calls, 0)
        action = failed.human_actions[-1]

        resolution = composition.resolve(
            action.id, HumanResolutionStrategy.RETRY_REPLANNING
        )
        staged = JsonProjectStateStore(state_file).load()
        self.assertIn("code-mule recover", "\n".join(resolution.output))
        self.assertEqual(execution.calls, 0)
        self.assertEqual(len(staged.plans), 1)
        self.assertEqual(len(staged.revisions), 1)

        restarted = ProductionCliComposition(
            state_file,
            environment={},
            stdout=StringIO(),
            stderr=StringIO(),
            runtime_factory=runtime_factory,
        )
        result = restarted.recover()
        final = JsonProjectStateStore(state_file).load()

        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(len(supervisor.requests), 2)
        self.assertEqual(execution.calls, 1)
        self.assertEqual(len(final.plans), 2)
        self.assertEqual(len(final.revisions), 2)
        self.assertEqual(final.plans[-1].change_request_id, "CR-2")
        self.assertEqual(final.revisions[-1].change_request_id, "CR-2")


class RevisionMaterializationRecoveryE2E(PostCompletionFailureFixture):
    def test_failed_contract_then_fresh_revision_completes_append_only(self):
        store, _ = self.fail(
            supervisor=SequenceSupervisor((self.lineage_conflict_proposal(),))
        )
        action = store.state.human_actions[-1]
        revision_one_before = serialize_project_state(store.state)["revisions"][0]
        plan_one_before = serialize_project_state(store.state)["plans"][0]
        tasks_before = serialize_project_state(store.state)["tasks"]

        HumanResolutionService(
            store,
            clock=Clock(NOW + timedelta(hours=4)),
            event_id_factory=EventIds(),
            resolution_id_factory=lambda: "resolution-retry",
        ).resolve(action.id, HumanResolutionStrategy.RETRY_REPLANNING)
        restarted = MemoryStore(
            deserialize_project_state(serialize_project_state(store.state))
        )
        supervisor = SequenceSupervisor((proposal_v2(),))
        outcome = PostCompletionReplanner(
            store=restarted,
            supervisor=supervisor,
            clock=Clock(NOW + timedelta(hours=5)),
            plan_id_factory=lambda: "PLAN-2",
            event_id_factory=EventIds(),
        ).replan(ChangeReplanningRequest("project-1", "CR-2"))
        materialized = restarted.state

        self.assertEqual(outcome.plan_version, 2)
        self.assertEqual(len(supervisor.requests), 1)
        self.assertEqual(len(materialized.plans), 2)
        self.assertEqual(len(materialized.revisions), 2)
        self.assertEqual(len(materialized.milestones), 2)
        self.assertEqual(len(materialized.tasks), 4)
        self.assertEqual(
            serialize_project_state(materialized)["revisions"][0],
            revision_one_before,
        )
        self.assertEqual(
            serialize_project_state(materialized)["plans"][0],
            plan_one_before,
        )
        self.assertEqual(
            serialize_project_state(materialized)["tasks"][:3], tasks_before
        )
        new_task = materialized.tasks[-1]
        self.assertEqual(new_task.milestone_id, "M2")
        self.assertEqual(new_task.supersedes_task_id, "T3")
        self.assertEqual(new_task.derived_from_task_ids, ())

        (self.root / "counter.js").write_text(
            "localStorage.setItem('counter', '1');\n"
        )
        subprocess.run(("git", "add", "counter.js"), cwd=self.root, check=True)
        subprocess.run(
            ("git", "commit", "-qm", "feat: persist counter"),
            cwd=self.root,
            check=True,
        )
        head_two = self.git("rev-parse", "HEAD")
        commit = GitCommitResult(
            new_task.id,
            str(self.root),
            self.head,
            head_two,
            "feat: persist counter",
            ("counter.js",),
            ("counter.js",),
            NOW + timedelta(hours=6),
        )
        ready = replace(
            materialized,
            tasks=materialized.tasks[:-1]
            + (replace(new_task, status=TaskStatus.COMPLETED),),
            git_commit_results=materialized.git_commit_results + (commit,),
        )
        final_store = MemoryStore(ready)
        final = ProjectFinalizationService(
            store=final_store,
            verification=ProjectVerificationService(
                clock=Clock(NOW + timedelta(hours=7)),
                result_id_factory=lambda: "verify-v2",
                environment={"PATH": "/usr/bin:/bin"},
            ),
            supervisor=ApprovingFinalSupervisor(),
            clock=Clock(NOW + timedelta(hours=8)),
            event_id_factory=EventIds(),
        ).finalize(ready)

        self.assertIs(final.project.status, ProjectStatus.DONE)
        self.assertEqual(len(final.plans), 2)
        self.assertEqual(len(final.revisions), 2)
        self.assertEqual(final.revisions[0].completion_head, self.head)
        self.assertEqual(final.revisions[1].completion_head, head_two)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "2")
        self.assertEqual(self.git("status", "--short"), "")


if __name__ == "__main__":
    unittest.main()
