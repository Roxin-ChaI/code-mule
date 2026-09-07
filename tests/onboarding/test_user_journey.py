"""Returning-Boss flows through fresh composition instances (new shell shape)."""

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from code_mule.cli.composition import ProductionCliComposition
from code_mule.cli.contracts import CliExitCode
from code_mule.domain.enums import (
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from code_mule.domain.models import Milestone, Plan, Task
from code_mule.progress import ConsoleProgressRenderer
from code_mule.recovery import SafePoint, SafePointKind
from code_mule.runtime import ProjectExecutionStopReason
from code_mule.state.store import JsonProjectStateStore

from tests.onboarding.helpers import chdir, git_repo
from tests.onboarding.test_start import (
    _FakeExecution,
    _FakePlanning,
    runtime_factory_for,
)


class ReturningBossJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = git_repo(self.root / "workspace")
        self.state_file = self.workspace / ".code-mule" / "project-state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def composition(self, workspace, *, runtime_factory=None):
        return ProductionCliComposition(
            workspace / ".code-mule" / "project-state.json",
            environment={},
            stdout=StringIO(),
            stderr=StringIO(),
            runtime_factory=runtime_factory,
        )

    def init(self, workspace):
        with chdir(workspace):
            composition = self.composition(workspace)
            result = composition.init_project("journey", "Journey", workspace)
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)

    def test_returning_boss_status_from_fresh_composition(self):
        self.init(self.workspace)
        with chdir(self.workspace):
            fresh = self.composition(self.workspace)
            result = fresh.status()
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        output = "\n".join(result.output)
        self.assertIn("PROJECT", output)
        self.assertIn("Journey", output)

    def test_diagnose_from_fresh_composition(self):
        self.init(self.workspace)
        with chdir(self.workspace):
            fresh = self.composition(self.workspace)
            result = fresh.diagnose()
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        output = "\n".join(result.output)
        self.assertIn("PROJECT DIAGNOSIS", output)
        self.assertIn("Project", output)
        self.assertIn("Ready", output)

    def test_recover_from_fresh_composition_after_plan_boundary(self):
        self.init(self.workspace)
        store = JsonProjectStateStore(self.state_file)
        _FakePlanning(store).plan(SimpleNamespace())
        now = datetime.now(UTC)
        current = store.load()
        store.save(
            replace(
                current,
                latest_safe_point=SafePoint(
                    SafePointKind.PLAN_MATERIALIZED,
                    now,
                ),
            )
        )
        with chdir(self.workspace):
            fresh = self.composition(
                self.workspace,
                runtime_factory=runtime_factory_for(self.state_file),
            )
            result = fresh.recover()
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        restored = JsonProjectStateStore(self.state_file).load()
        self.assertIs(restored.project.status, ProjectStatus.DONE)
        self.assertEqual(
            len(restored.execution_leases),
            1,
        )
        self.assertIn("RECOVERY", "\n".join(result.output))


if __name__ == "__main__":
    unittest.main()
