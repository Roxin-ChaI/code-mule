from dataclasses import replace
from io import StringIO
from unittest import TestCase
from unittest.mock import patch

from code_mule.domain import HumanAction, HumanActionCategory, HumanActionStatus, ProjectStatus, TaskStatus, WorkerInputDetails
from code_mule.diagnosis import ProjectDiagnosisService
from code_mule.presentation import render_project, render_project_diagnosis, render_human_action
from code_mule.presentation.panels import recovery_dashboard
from code_mule.presentation.terminal import TerminalDashboard, display_width
from code_mule.recovery import RecoveryMode, RecoveryPlan, BoundaryRecoverability, SafePointKind
from code_mule.cli.app import _print_error
from code_mule.cli.contracts import CliRecoveryRequired
from code_mule.state.serialization import serialize_project_state
from state import make_project_state, CREATED


def action(category):
    details = WorkerInputDetails("worker/report", "request-1", "请选择排行榜存储方案", ("localStorage", "session memory"), 1) if category is HumanActionCategory.WORKER_INPUT else None
    return HumanAction("action-1", "project-1", "task-1", category, "Boss decision required", "Inspect this boundary", "Execution stopped safely", HumanActionStatus.PENDING, CREATED, worker_input=details)


def recovery():
    return RecoveryPlan(RecoveryMode.RESUME_PLAN, SafePointKind.PLAN_MATERIALIZED, BoundaryRecoverability.RECOVERABLE, False, True, False, True, "Use the existing Plan", "code-mule recover")


class PanelIntegrationTests(TestCase):
    def test_project_state_matrix_responsive_and_plain_compatible(self):
        for status in (ProjectStatus.IDLE, ProjectStatus.RUNNING, ProjectStatus.HUMAN_REQUIRED, ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.CHANGE_REQUESTED, ProjectStatus.DONE, ProjectStatus.CANCELLED):
            source = make_project_state()
            state = replace(source, project=replace(source.project, status=status))
            before = serialize_project_state(state)
            for width in (40, 60, 80, 120):
                with self.subTest(status=status, width=width):
                    lines = render_project(state, terminal=TerminalDashboard(width, True))
                    self.assertTrue(all(display_width(line) <= width for line in lines))
                    self.assertIn("Status", "\n".join(lines))
                    self.assertNotIn("project_id:", "\n".join(lines))
                    self.assertEqual(render_project(state), render_project(state, terminal=TerminalDashboard(width)))
            self.assertEqual(before, serialize_project_state(state))

    def test_idle_screenshot_content_golden(self):
        source = make_project_state()
        state = replace(source, project=replace(source.project, name="My Project", status=ProjectStatus.IDLE, active_plan_id=None, current_task_id=None))
        lines = render_project(state, terminal=TerminalDashboard(40, True))
        content = tuple(line[2:-2].rstrip() for line in lines if line.startswith("│"))
        self.assertEqual(
            content,
            (
                "CODE MULE · My Project",
                "Project        My Project",
                "Status         Ready",
                "Revision       —",
                "Plan           —",
                "Progress       0 / 0",
                "Current        None",
                "Safe Point     Unknown",
            ),
        )

    def test_all_human_categories_keep_commands_and_hide_internal_ids(self):
        for category in (HumanActionCategory.WORKER_INPUT, HumanActionCategory.WORKER_APPROVAL, HumanActionCategory.EXTERNAL_SIDE_EFFECT, HumanActionCategory.WORKER_VERIFICATION, HumanActionCategory.WORKSPACE_BLOCK, HumanActionCategory.RECOVERY_UNCERTAIN, HumanActionCategory.FINAL_REVIEW_DECISION):
            value = action(category)
            for width in (40, 60, 80, 120):
                with self.subTest(category=category, width=width):
                    lines = render_human_action(value, state=make_project_state(), terminal=TerminalDashboard(width, True))
                    output = "\n".join(lines)
                    self.assertIn("ACTION REQUIRED", output)
                    self.assertIn("code-mule", output)
                    self.assertNotIn("request-1", output)
                    self.assertNotIn("created_at", output)
                    self.assertTrue(all(display_width(line) <= width for line in lines))
                    self.assertEqual(render_human_action(value), render_human_action(value, terminal=TerminalDashboard(width)))

    def test_status_shows_only_nonempty_boss_panel(self):
        source = make_project_state()
        self.assertNotIn("BOSS ACTION", "\n".join(render_project(source, terminal=TerminalDashboard(80, True))))
        state = replace(source, human_actions=(action(HumanActionCategory.WORKER_INPUT),))
        text = "\n".join(render_project(state, terminal=TerminalDashboard(80, True)))
        self.assertIn("BOSS ACTION", text)
        self.assertIn("code-mule inspect", text)

    def test_diagnosis_projection_preserves_facts(self):
        diagnosis = ProjectDiagnosisService().diagnose(make_project_state())
        for width in (40, 60, 80, 120):
            lines = render_project_diagnosis(diagnosis, terminal=TerminalDashboard(width, True))
            output = "\n".join(lines)
            self.assertIn("DIAGNOSIS", output)
            self.assertIn("NEXT ACTION", output)
            self.assertTrue(all(display_width(line) <= width for line in lines))
            self.assertEqual(render_project_diagnosis(diagnosis), render_project_diagnosis(diagnosis, terminal=TerminalDashboard(width)))

    def test_recovery_ready_and_blocked_share_layout(self):
        class TTY(StringIO):
            def isatty(self): return True
        for width in (40, 60, 80, 120):
            lines = recovery_dashboard(make_project_state(), recovery(), TerminalDashboard(width, True))
            self.assertIn("RECOVERY READY", "\n".join(lines))
            self.assertTrue(all(display_width(line) <= width for line in lines))
            stream = TTY()
            with patch("code_mule.cli.app.TerminalDashboard.for_stream", return_value=TerminalDashboard(width, True)):
                _print_error(CliRecoveryRequired("Worker outcome is uncertain"), stream)
            self.assertIn("RECOVERY BLOCKED", stream.getvalue())
            self.assertIn("Worker outcome", stream.getvalue())
            self.assertNotIn("RECOVERY READY", stream.getvalue())
        stream = StringIO()
        _print_error(CliRecoveryRequired("Worker outcome is uncertain"), stream)
        self.assertTrue(stream.getvalue().startswith("RECOVERY REQUIRED\n"))
        self.assertNotIn("\x1b", stream.getvalue())

    def test_verbose_has_separate_internal_section(self):
        text = "\n".join(render_project(make_project_state(), verbose=True, terminal=TerminalDashboard(120, True)))
        self.assertIn("INTERNAL", text)
        self.assertIn("project_id:", text)

    def test_completion_keeps_skipped_distinct_from_pass(self):
        from code_mule.project_verification import ProjectVerificationResult, ProjectVerificationCheck, ProjectVerificationCategory, ProjectVerificationStatus, FinalReviewDecision
        source = make_project_state()
        check = ProjectVerificationCheck("Lint", ProjectVerificationCategory.LINT, (), ProjectVerificationStatus.SKIPPED, None, "Not configured", False)
        result = ProjectVerificationResult("verify-1", source.project.id, source.project.active_plan_id, "a" * 40, "a" * 40, (check,), CREATED, CREATED, FinalReviewDecision.APPROVE, "Approved")
        state = replace(source, project=replace(source.project, status=ProjectStatus.DONE), project_verification_results=(result,))
        output = "\n".join(render_project(state, terminal=TerminalDashboard(80, True)))
        self.assertIn("PROJECT COMPLETED", output)
        self.assertIn("Lint           Skipped", output)
        self.assertNotIn("Git            Clean", output)
