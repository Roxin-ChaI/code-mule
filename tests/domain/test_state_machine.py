import unittest
from itertools import product

from code_mule.domain import state_machine
from code_mule.domain.enums import ProjectStatus
from code_mule.domain.state_machine import (
    InvalidProjectTransition,
    can_transition,
    validate_transition,
)


EXPECTED_TRANSITIONS = {
    (ProjectStatus.IDLE, ProjectStatus.PLANNING),
    (ProjectStatus.IDLE, ProjectStatus.CANCELLED),
    (ProjectStatus.PLANNING, ProjectStatus.RUNNING),
    (ProjectStatus.PLANNING, ProjectStatus.HUMAN_REQUIRED),
    (ProjectStatus.PLANNING, ProjectStatus.FAILED),
    (ProjectStatus.PLANNING, ProjectStatus.CANCELLED),
    (ProjectStatus.RUNNING, ProjectStatus.RUNNING),
    (ProjectStatus.RUNNING, ProjectStatus.CHANGE_REQUESTED),
    (ProjectStatus.RUNNING, ProjectStatus.PAUSED_BY_BOSS),
    (ProjectStatus.RUNNING, ProjectStatus.HUMAN_REQUIRED),
    (ProjectStatus.RUNNING, ProjectStatus.DONE),
    (ProjectStatus.RUNNING, ProjectStatus.FAILED),
    (ProjectStatus.RUNNING, ProjectStatus.CANCEL_REQUESTED),
    (ProjectStatus.RUNNING, ProjectStatus.CANCELLED),
    (ProjectStatus.CHANGE_REQUESTED, ProjectStatus.REPLANNING),
    (ProjectStatus.CHANGE_REQUESTED, ProjectStatus.HUMAN_REQUIRED),
    (ProjectStatus.CHANGE_REQUESTED, ProjectStatus.FAILED),
    (ProjectStatus.CHANGE_REQUESTED, ProjectStatus.CANCEL_REQUESTED),
    (ProjectStatus.CHANGE_REQUESTED, ProjectStatus.CANCELLED),
    (ProjectStatus.REPLANNING, ProjectStatus.RUNNING),
    (ProjectStatus.REPLANNING, ProjectStatus.HUMAN_REQUIRED),
    (ProjectStatus.REPLANNING, ProjectStatus.FAILED),
    (ProjectStatus.REPLANNING, ProjectStatus.CANCELLED),
    (ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.RUNNING),
    (ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.CHANGE_REQUESTED),
    (ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.HUMAN_REQUIRED),
    (ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.CANCEL_REQUESTED),
    (ProjectStatus.PAUSED_BY_BOSS, ProjectStatus.CANCELLED),
    (ProjectStatus.HUMAN_REQUIRED, ProjectStatus.RUNNING),
    (ProjectStatus.HUMAN_REQUIRED, ProjectStatus.PAUSED_BY_BOSS),
    (ProjectStatus.HUMAN_REQUIRED, ProjectStatus.FAILED),
    (ProjectStatus.HUMAN_REQUIRED, ProjectStatus.CANCELLED),
    (ProjectStatus.CANCEL_REQUESTED, ProjectStatus.CANCELLED),
    (ProjectStatus.CANCEL_REQUESTED, ProjectStatus.HUMAN_REQUIRED),
    (ProjectStatus.DONE, ProjectStatus.CHANGE_REQUESTED),
}


class ProjectStateMachineTests(unittest.TestCase):
    def test_can_transition_for_every_status_pair(self):
        for current, target in product(ProjectStatus, repeat=2):
            with self.subTest(current=current, target=target):
                self.assertEqual(
                    can_transition(current, target),
                    (current, target) in EXPECTED_TRANSITIONS,
                )

    def test_validate_transition_for_every_status_pair(self):
        for current, target in product(ProjectStatus, repeat=2):
            with self.subTest(current=current, target=target):
                if (current, target) in EXPECTED_TRANSITIONS:
                    self.assertIsNone(validate_transition(current, target))
                else:
                    with self.assertRaises(InvalidProjectTransition) as context:
                        validate_transition(current, target)
                    self.assertIn(current.value, str(context.exception))
                    self.assertIn(target.value, str(context.exception))

    def test_terminal_statuses_have_no_outgoing_transitions(self):
        for current in (ProjectStatus.FAILED, ProjectStatus.CANCELLED):
            for target in ProjectStatus:
                with self.subTest(current=current, target=target):
                    self.assertFalse(can_transition(current, target))

        self.assertTrue(
            can_transition(
                ProjectStatus.DONE,
                ProjectStatus.CHANGE_REQUESTED,
            )
        )

    def test_running_to_running_is_legal(self):
        self.assertTrue(can_transition(ProjectStatus.RUNNING, ProjectStatus.RUNNING))
        self.assertIsNone(validate_transition(ProjectStatus.RUNNING, ProjectStatus.RUNNING))

    def test_query_is_not_part_of_state_machine_api(self):
        self.assertNotIn("QUERY", state_machine.__all__)
        self.assertNotIn("BossCommandType", state_machine.__all__)
        self.assertFalse(hasattr(state_machine, "QUERY"))


if __name__ == "__main__":
    unittest.main()
