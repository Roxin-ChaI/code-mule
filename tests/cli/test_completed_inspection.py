"""Completed-project inspection is a read-only success path."""

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli.composition import ProductionCliComposition
from code_mule.cli.contracts import CliExitCode
from code_mule.domain.enums import (
    PlanStatus,
    ProjectStatus,
    RevisionCheckStatus,
    RevisionStatus,
    TaskStatus,
)
from code_mule.domain.models import ProjectRevision
from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCheck,
    ProjectVerificationResult,
    ProjectVerificationStatus,
)
from code_mule.runtime_handoff import (
    DeliverableType,
    DeliveryManifest,
    DeliveryManifestStatus,
    HealthCheckSpec,
    HealthCheckType,
    RuntimeHealthStatus,
    RuntimeSession,
    RuntimeSessionStatus,
    VerificationSpec,
)
from code_mule.state.store import JsonProjectStateStore
from tests.state import make_project_state


NOW = datetime(2026, 9, 21, tzinfo=UTC)


def completed_state():
    state = make_project_state()
    plan = replace(state.plans[0], status=PlanStatus.COMPLETED)
    tasks = tuple(replace(task, status=TaskStatus.COMPLETED) for task in state.tasks)
    revision = ProjectRevision(
        1,
        state.project.created_at,
        RevisionStatus.COMPLETED,
        plan.id,
        plan.version,
        completed_at=NOW,
        baseline_head="a" * 40,
        completion_head="b" * 40,
        verification_status=RevisionCheckStatus.PASS,
        final_review_status=RevisionCheckStatus.PASS,
        verification_result_id="verification-1",
    )
    check = ProjectVerificationCheck(
        "tests",
        ProjectVerificationCategory.TEST,
        ("python", "-m", "unittest"),
        ProjectVerificationStatus.PASS,
        0,
        "passed",
        True,
    )
    verification = ProjectVerificationResult(
        "verification-1",
        state.project.id,
        plan.id,
        "b" * 40,
        "b" * 40,
        (check,),
        NOW,
        NOW,
        FinalReviewDecision.APPROVE,
        "approved",
    )
    return replace(
        state,
        project=replace(
            state.project,
            status=ProjectStatus.DONE,
            current_task_id=None,
        ),
        plans=(plan,),
        tasks=tasks,
        human_actions=(),
        revisions=(revision,),
        project_verification_results=(verification,),
        delivery_manifest_required=False,
    )


def manifest(state):
    return DeliveryManifest(
        "manifest-r1-1",
        state.project.id,
        1,
        1,
        DeliverableType.DOCUMENTATION,
        False,
        "README.md",
        None,
        VerificationSpec(("README.md",), False),
        HealthCheckSpec(HealthCheckType.NONE),
        None,
        None,
        (),
        (),
        "Read README.md.",
        NOW,
        NOW,
        DeliveryManifestStatus.VERIFIED,
    )


def session(state, status):
    return RuntimeSession(
        "runtime-1",
        state.project.id,
        1,
        "manifest-r1-1",
        4242 if status is RuntimeSessionStatus.RUNNING else None,
        NOW,
        status,
        "http://127.0.0.1:8080/" if status is RuntimeSessionStatus.RUNNING else None,
        RuntimeHealthStatus.HEALTHY if status is RuntimeSessionStatus.RUNNING else RuntimeHealthStatus.NOT_CHECKED,
        None,
        None,
        0 if status is RuntimeSessionStatus.STOPPED else None,
        NOW if status is RuntimeSessionStatus.STOPPED else None,
    )


class CompletedInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "project-state.json"
        self.output = StringIO()

    def tearDown(self):
        self.temporary.cleanup()

    def inspect(self, state, *, verbose=False):
        store = JsonProjectStateStore(self.path)
        store.save(state)
        before = self.path.read_bytes()
        composition = ProductionCliComposition(
            self.path,
            environment={},
            stdout=self.output,
            stderr=StringIO(),
        )
        result = composition.inspect(verbose=verbose)
        self.assertEqual(self.path.read_bytes(), before)
        return result

    def test_done_without_human_action_is_a_read_only_success(self):
        result = self.inspect(completed_state())
        text = "\n".join(result.output)
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertIn("PROJECT INSPECTION", text)
        self.assertIn("PROJECT COMPLETED", text)
        self.assertIn("FINAL VERIFICATION", text)
        self.assertIn("Human action None", text)

    def test_done_manifest_is_visible(self):
        state = completed_state()
        state = replace(
            state,
            delivery_manifest_required=True,
            delivery_manifests=(manifest(state),),
        )
        text = "\n".join(self.inspect(state).output)
        self.assertIn("DELIVERY HANDOFF", text)
        self.assertIn("README.md", text)

    def test_done_running_and_stopped_runtime_are_visible(self):
        for status in (RuntimeSessionStatus.RUNNING, RuntimeSessionStatus.STOPPED):
            with self.subTest(status=status):
                state = completed_state()
                state = replace(state, runtime_sessions=(session(state, status),))
                text = "\n".join(self.inspect(state, verbose=True).output)
                self.assertIn("RUNTIME", text)
                self.assertIn(status.value.title(), text)
                self.assertIn("runtime-1", text)


if __name__ == "__main__":
    unittest.main()
