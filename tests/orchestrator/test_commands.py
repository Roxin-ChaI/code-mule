import unittest
from dataclasses import FrozenInstanceError

from code_mule.domain.enums import ProjectStatus
from code_mule.domain.models import QualityStatus
from code_mule.orchestrator.commands import (
    ChangeCommand,
    PauseCommand,
    QueryCommand,
    ResumeCommand,
    StopCommand,
)
from code_mule.orchestrator.results import (
    ChangeResult,
    CommandResult,
    ProjectStatusView,
    StopResult,
)


class BossCommandContractTests(unittest.TestCase):
    def test_valid_commands_preserve_explicit_values(self):
        query = QueryCommand("project-1")
        change = ChangeCommand("project-1", "Add requirement", "boss", "change-1")
        pause = PauseCommand("project-1")
        resume = ResumeCommand("project-1")
        stop = StopCommand("project-1")

        self.assertEqual(query.project_id, "project-1")
        self.assertEqual(change.change_request_id, "change-1")
        self.assertEqual(change.description, "Add requirement")
        self.assertEqual(change.created_by, "boss")
        self.assertEqual(pause.project_id, "project-1")
        self.assertEqual(resume.project_id, "project-1")
        self.assertEqual(stop.reason, "Boss requested project cancellation")

    def test_empty_project_id_is_rejected_for_every_command(self):
        constructors = (
            lambda: QueryCommand(""),
            lambda: ChangeCommand("", "description", "boss", "change-1"),
            lambda: PauseCommand(""),
            lambda: ResumeCommand(""),
            lambda: StopCommand(""),
        )
        for construct in constructors:
            with self.subTest(command=construct):
                with self.assertRaises(ValueError):
                    construct()

    def test_empty_change_fields_are_rejected(self):
        for field in ("description", "created_by", "change_request_id"):
            values = {
                "project_id": "project-1",
                "description": "description",
                "created_by": "boss",
                "change_request_id": "change-1",
            }
            values[field] = ""
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    ChangeCommand(**values)

    def test_commands_do_not_strip_or_replace_input(self):
        command = ChangeCommand(" project ", " description ", " boss ", " change ")
        self.assertEqual(command.project_id, " project ")
        self.assertEqual(command.description, " description ")
        self.assertEqual(command.created_by, " boss ")
        self.assertEqual(command.change_request_id, " change ")

    def test_commands_are_frozen(self):
        command = QueryCommand("project-1")
        with self.assertRaises(FrozenInstanceError):
            command.project_id = "project-2"


class ResultContractTests(unittest.TestCase):
    def test_status_view_is_typed_data_without_persistence_details(self):
        quality = QualityStatus("passed", "passed", "passed", "passed", True)
        view = ProjectStatusView(
            "project-1",
            "Code Mule",
            ProjectStatus.RUNNING,
            "plan-1",
            "task-1",
            6,
            1,
            1,
            1,
            1,
            2,
            quality,
        )
        self.assertEqual(view.total_tasks, 6)
        self.assertIs(view.quality_status, quality)
        self.assertNotIn("store", vars(view))

    def test_command_and_change_results_preserve_control_fields(self):
        command_result = CommandResult(
            "project-1",
            ProjectStatus.RUNNING,
            ProjectStatus.PAUSED_BY_BOSS,
            True,
            "event-1",
            "Project paused",
        )
        change_result = ChangeResult(
            "project-1",
            "change-1",
            ProjectStatus.RUNNING,
            ProjectStatus.CHANGE_REQUESTED,
            "event-2",
        )
        self.assertTrue(command_result.state_changed)
        self.assertEqual(command_result.current_status, ProjectStatus.PAUSED_BY_BOSS)
        self.assertEqual(change_result.change_request_id, "change-1")
        self.assertEqual(change_result.current_status, ProjectStatus.CHANGE_REQUESTED)
        stop_result = StopResult(
            "project-1", ProjectStatus.RUNNING, ProjectStatus.CANCEL_REQUESTED,
            True, True, ("event-3",), "Cancellation requested",
        )
        self.assertTrue(stop_result.safe_point_required)


if __name__ == "__main__":
    unittest.main()
