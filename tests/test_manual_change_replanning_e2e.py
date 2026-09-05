from dataclasses import replace
import inspect
import unittest
from unittest.mock import patch

from code_mule.domain.enums import ProjectStatus, SupervisorDecisionType, TaskStatus
from code_mule.domain.models import Decision, ExecutionReport, ProjectEvent
from code_mule.replanning import ChangeReplanMaterializer, ChangeReplanValidator
from code_mule.runtime import ProjectExecutionOutcome, ProjectExecutionStopReason
from scripts import manual_change_replanning_e2e as manual
from replanning.test_materialization import replanning_state
from replanning.test_validation import NOW, valid_proposal


class ManualChangeReplanningE2ETests(unittest.TestCase):
    def test_replacement_plan_diagnostics_expose_graph_and_ready_task(self):
        state = replanning_state()
        materialized = ChangeReplanMaterializer(
            ChangeReplanValidator()
        ).materialize(
            state,
            state.change_requests[0],
            valid_proposal(),
            plan_id="PLAN-2",
            operation_time=NOW,
        )

        diagnostics = manual._replacement_plan_diagnostics(materialized)

        self.assertEqual(diagnostics["replacement_active_plan_id"], "PLAN-2")
        self.assertEqual(diagnostics["replacement_plan_status"], "active")
        self.assertEqual(diagnostics["replacement_plan_version"], 2)
        self.assertEqual(
            diagnostics["replacement_task_ids"],
            ["T1", "T2", "T3"],
        )
        self.assertEqual(
            diagnostics["replacement_task_statuses"],
            {"T1": "completed", "T2": "pending", "T3": "pending"},
        )
        self.assertTrue(diagnostics["dependencies_valid"])
        self.assertTrue(diagnostics["traceability_valid"])
        self.assertTrue(diagnostics["ready_task_available"])
        self.assertEqual(diagnostics["ready_task_id_before_resume"], "T2")

    def test_human_required_diagnostics_preserve_typed_task_cycle_reason(self):
        state = replanning_state()
        task = replace(state.tasks[1], status=TaskStatus.IN_PROGRESS)
        report = ExecutionReport(
            "report-human",
            task.id,
            1,
            "completed",
            (),
            ("tests passed",),
            (),
            "clean",
            (),
            None,
            "worker completed",
            NOW,
        )
        decision = Decision(
            "decision-human",
            task.id,
            SupervisorDecisionType.HUMAN_REQUIRED,
            "human review required",
            NOW,
        )
        event = ProjectEvent(
            "event-human",
            state.project.id,
            "task.human_required",
            task.id,
            NOW,
            {"source": "supervisor"},
        )
        final = replace(
            state,
            project=replace(
                state.project,
                status=ProjectStatus.HUMAN_REQUIRED,
                current_task_id=task.id,
            ),
            tasks=(state.tasks[0], task),
            execution_reports=(report,),
            decisions=(decision,),
            events=state.events + (event,),
        )
        execution = ProjectExecutionOutcome(
            state.project.id,
            1,
            0,
            (task.id,),
            ProjectStatus.HUMAN_REQUIRED,
            False,
            True,
            ProjectExecutionStopReason.HUMAN_REQUIRED,
        )

        diagnostics = manual._human_required_diagnostics(execution, final)

        self.assertEqual(diagnostics["human_required_reason"], "supervisor")
        self.assertEqual(diagnostics["human_required_task_id"], task.id)
        self.assertEqual(diagnostics["human_required_attempt"], 1)
        self.assertEqual(
            diagnostics["human_required_final_decision"], "human_required"
        )
        self.assertEqual(
            diagnostics["human_required_failure_category"], "supervisor"
        )

    def test_no_runnable_stop_reason_is_reported_without_string_parsing(self):
        state = replanning_state()
        final = replace(
            state,
            project=replace(state.project, status=ProjectStatus.HUMAN_REQUIRED),
        )
        execution = ProjectExecutionOutcome(
            state.project.id,
            0,
            0,
            (),
            ProjectStatus.HUMAN_REQUIRED,
            False,
            True,
            ProjectExecutionStopReason.NO_RUNNABLE_TASK,
        )

        diagnostics = manual._human_required_diagnostics(execution, final)

        self.assertEqual(
            diagnostics["human_required_reason"], "no_runnable_task"
        )
        self.assertEqual(
            diagnostics["human_required_failure_category"],
            "no_runnable_task",
        )

    def test_missing_key_stops_before_real_composition(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(manual.main(), 2)

    def test_transport_keeps_tls_defaults_and_no_output_cap(self):
        config = manual._build_supervisor_config("deepseek-test")
        source = inspect.getsource(manual._build_compatibility_client)
        self.assertIsNone(config.max_output_tokens)
        self.assertIn("max_retries=0", source)
        self.assertIn("trust_env=False", source)
        self.assertNotIn("verify=False", source)

    def test_manual_path_uses_real_planning_replanning_and_disposable_repo(self):
        source = inspect.getsource(manual)
        self.assertIn(
            "REAL DEEPSEEK + CODEX CHANGE REPLANNING — MANUAL ONLY",
            source,
        )
        self.assertIn("ProjectPlanningService", source)
        self.assertIn("ChangeReplanningService", source)
        self.assertIn("ChangeExecutionService", source)
        self.assertIn("TemporaryDirectory", source)
        self.assertNotIn("Requirement(", source)
        self.assertNotIn("Task(", source)


if __name__ == "__main__":
    unittest.main()
