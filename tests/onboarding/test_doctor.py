from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli.composition import ProductionCliComposition
from code_mule.cli.contracts import CliExitCode
from code_mule.domain.models import Project, ProjectStatus
from code_mule.onboarding import DoctorService, render_doctor
from code_mule.state.models import ProjectState
from code_mule.recovery import SafePoint, SafePointKind

from tests.onboarding.helpers import (
    chdir,
    fake_code_mule,
    fake_codex,
    git_repo,
    git_system_paths,
)


def _state(project_id: str, name: str, workspace: str) -> ProjectState:
    now = datetime.now(UTC)
    return ProjectState(
        project=Project(
            project_id,
            name,
            ProjectStatus.IDLE,
            None,
            None,
            now,
            now,
            workspace,
        ),
        requirements=(),
        plans=(),
        milestones=(),
        tasks=(),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
        latest_safe_point=SafePoint(SafePointKind.PROJECT_IDLE, now),
    )


class DoctorServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = git_repo(self.root / "workspace")
        self.bin = self.root / "bin"
        fake_codex(self.bin)
        fake_code_mule(self.bin)

    def tearDown(self):
        self.temporary.cleanup()

    def environment(self, *, codex=True, code_mule=True, git=True, key=True):
        self.bin.mkdir(parents=True, exist_ok=True)
        for existing in ("codex", "code-mule"):
            candidate = self.bin / existing
            if candidate.exists():
                candidate.unlink()
        if codex:
            fake_codex(self.bin)
        if code_mule:
            fake_code_mule(self.bin)
        parts: list[str] = []
        if git:
            parts.append(git_system_paths())
        if codex or code_mule:
            parts.insert(0, str(self.bin))
        environment = {"PATH": ":".join(parts) or git_system_paths()}
        if key:
            environment["DEEPSEEK_API_KEY"] = "sk-doctor-secret"
        return environment

    def report(self, environment, state_file=None):
        service = DoctorService(environment=environment)
        return service.diagnose(
            self.workspace,
            state_file or self.workspace / ".code-mule" / "project-state.json",
        )

    def test_doctor_healthy_with_all_rows_ready(self):
        environment = self.environment()
        report = self.report(environment)
        self.assertTrue(report.healthy)
        self.assertEqual(report.problem_count, 0)
        self.assertEqual(report.check("Code Mule").status, "PASS")
        self.assertEqual(report.check("Python").status, "PASS")
        self.assertEqual(report.check("Git").status, "PASS")
        self.assertEqual(report.check("Codex").status, "PASS")
        self.assertEqual(report.check("DeepSeek").status, "CONFIGURED")
        self.assertEqual(report.check("Workspace").status, "READY")
        rendered = "\n".join(render_doctor(report))
        self.assertIn("ALL CHECKS PASSED", rendered)
        self.assertNotIn("sk-doctor-secret", rendered)

    def test_doctor_missing_git_fails_closed(self):
        environment = self.environment(git=False)
        report = self.report(environment)
        self.assertFalse(report.healthy)
        self.assertEqual(report.check("Git").status, "MISSING")
        self.assertEqual(report.check("Workspace").status, "PROBLEM")
        rendered = "\n".join(render_doctor(report))
        self.assertIn("Install Git", rendered)

    def test_doctor_missing_codex_fails_closed(self):
        environment = self.environment(codex=False)
        report = self.report(environment)
        self.assertFalse(report.healthy)
        self.assertEqual(report.check("Codex").status, "MISSING")
        rendered = "\n".join(render_doctor(report))
        self.assertIn("Install the Codex CLI", rendered)

    def test_doctor_does_not_gate_lower_codex_versions(self):
        fake_codex(self.bin, version="0.99.0")
        fake_code_mule(self.bin)
        environment = {
            "PATH": git_system_paths() + ":" + str(self.bin),
            "DEEPSEEK_API_KEY": "sk-doctor-secret",
        }
        report = self.report(environment)
        self.assertTrue(report.check("Codex").healthy)
        rendered = "\n".join(render_doctor(report, verbose=True))
        self.assertIn("0.99.0", rendered)

    def test_doctor_missing_deepseek_key_is_not_configured(self):
        environment = self.environment(key=False)
        report = self.report(environment)
        self.assertFalse(report.healthy)
        self.assertEqual(
            report.check("DeepSeek").status,
            "NOT CONFIGURED",
        )
        rendered = "\n".join(render_doctor(report))
        self.assertIn("DEEPSEEK_API_KEY", rendered)
        self.assertNotIn("sk-doctor-secret", rendered)

    def test_doctor_dirty_workspace_is_problem(self):
        (self.workspace / "dirty.txt").write_text("change", encoding="utf-8")
        report = self.report(self.environment())
        self.assertEqual(report.check("Workspace").status, "PROBLEM")
        self.assertFalse(report.check("Workspace").healthy)
        rendered = "\n".join(render_doctor(report))
        self.assertIn("never stashes, resets, or cleans", rendered)

    def test_doctor_state_present_shows_project_status(self):
        from code_mule.git_delivery import register_state_exclusion
        from code_mule.state.store import JsonProjectStateStore

        state_file = self.workspace / ".code-mule" / "project-state.json"
        state_file.parent.mkdir()
        JsonProjectStateStore(state_file).save(
            _state("probe", "Probe", str(self.workspace))
        )
        register_state_exclusion(self.workspace, state_file)
        report = self.report(self.environment(), state_file=state_file)
        self.assertTrue(report.healthy)
        rendered = "\n".join(render_doctor(report, verbose=True))
        self.assertIn("Probe", rendered)
        self.assertIn("status    idle", rendered)

    def test_verbose_doctor_never_prints_secret_or_raw_codex_output(self):
        environment = self.environment()
        report = self.report(environment)
        rendered = "\n".join(render_doctor(report, verbose=True))
        self.assertNotIn("sk-doctor-secret", rendered)
        self.assertIn("version  0.1.1", rendered)

    def test_doctor_via_composition_starts_no_runtime_and_writes_nothing(self):
        calls = []

        def forbidden_runtime(state):
            calls.append("runtime")
            raise AssertionError("doctor must not compose runtime")

        with chdir(self.workspace):
            stdout = StringIO()
            composition = ProductionCliComposition(
                self.workspace / ".code-mule" / "project-state.json",
                environment=self.environment(),
                stdout=stdout,
                stderr=StringIO(),
                runtime_factory=forbidden_runtime,
            )
            result = composition.doctor()
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(calls, [])
        self.assertFalse(
            (self.workspace / ".code-mule" / "execution.lock").exists()
        )


if __name__ == "__main__":
    unittest.main()
