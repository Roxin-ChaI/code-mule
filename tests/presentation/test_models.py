from dataclasses import replace
import unittest

from code_mule.domain import (
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.presentation import (
    decision_label,
    project_view,
    render_project,
    status_label,
)
from state import make_project_state


class PresentationModelTests(unittest.TestCase):
    def test_human_readable_status_and_decision_mapping(self):
        expected = {
            ProjectStatus.RUNNING: "Running",
            ProjectStatus.CHANGE_REQUESTED: "Change requested",
            ProjectStatus.REPLANNING: "Replanning",
            ProjectStatus.HUMAN_REQUIRED: "Action required",
            ProjectStatus.DONE: "Completed",
            ProjectStatus.FAILED: "Failed",
        }
        for value, label in expected.items():
            with self.subTest(value=value):
                self.assertEqual(status_label(value), label)
        self.assertEqual(
            decision_label(SupervisorDecisionType.CONTINUE), "Approved"
        )
        self.assertEqual(
            decision_label(SupervisorDecisionType.REWORK), "Changes requested"
        )

    def test_project_view_uses_active_plan_and_deterministic_task_progress(self):
        state = make_project_state()
        view = project_view(state)
        self.assertEqual(view.name, "Code Mule")
        self.assertEqual(view.plan_version, 1)
        self.assertEqual((view.completed_tasks, view.total_tasks), (0, 1))
        self.assertEqual(view.current_task, "task-1 · Serialize state")
        cancelled = replace(state.tasks[0], status=TaskStatus.CANCELLED)
        view = project_view(replace(state, tasks=(cancelled,)))
        self.assertEqual((view.completed_tasks, view.total_tasks), (0, 1))

    def test_default_hides_internal_ids_and_verbose_exposes_them(self):
        state = make_project_state()
        default = "\n".join(render_project(state))
        self.assertIn("Status      Running", default)
        self.assertIn("Plan        v1", default)
        self.assertIn("Progress    0 / 1", default)
        self.assertNotIn("active_plan_id", default)
        self.assertNotIn("project_status: running", default)
        verbose = "\n".join(render_project(state, verbose=True))
        self.assertIn("project_id: project-1", verbose)
        self.assertIn("active_plan_id: plan-1", verbose)
        self.assertIn("current_task_id: task-1", verbose)
        self.assertIn("project_status: running", verbose)


if __name__ == "__main__":
    unittest.main()
