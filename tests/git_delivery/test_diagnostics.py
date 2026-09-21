from dataclasses import replace
import unittest

from code_mule.domain import (
    HumanAction,
    HumanActionCategory,
    HumanActionStatus,
    ProjectEvent,
    ProjectStatus,
)
from code_mule.git_delivery import (
    GitDeliveryFailureCode,
    GitDeliveryFailureDetails,
    GitOwnershipError,
    GitOwnershipStatus,
    git_delivery_failure_evidence,
    git_delivery_failure_metadata,
)
from state import UPDATED, make_project_state


class GitDeliveryDiagnosticsTests(unittest.TestCase):
    def details(self):
        return GitDeliveryFailureDetails(
            GitDeliveryFailureCode.EXPECTED_PATHS_MISMATCH,
            "a" * 40,
            "a" * 40,
            ("expected.py",),
            ("actual.py",),
            (),
            None,
            GitOwnershipStatus.MISMATCH,
            False,
            "Worker-owned paths do not match the repository change set.",
        )

    def state(self, metadata):
        state = make_project_state()
        action = HumanAction(
            "action-git",
            state.project.id,
            state.tasks[0].id,
            HumanActionCategory.RECOVERY_UNCERTAIN,
            "Git delivery stopped safely",
            "Inspect repository ownership",
            "No uncertain commit may be retried automatically",
            HumanActionStatus.PENDING,
            UPDATED,
        )
        event = ProjectEvent(
            "event-git",
            state.project.id,
            "git.delivery_failed",
            state.tasks[0].id,
            UPDATED,
            metadata,
        )
        return replace(
            state,
            project=replace(state.project, status=ProjectStatus.HUMAN_REQUIRED),
            human_actions=(action,),
            events=state.events + (event,),
        ), action

    def test_safe_metadata_round_trips_without_exception_message(self):
        error = GitOwnershipError(
            "token=secret raw Git failure", details=self.details()
        )
        metadata = git_delivery_failure_metadata(error, "ownership")
        self.assertNotIn("secret", repr(metadata))
        self.assertEqual(metadata["failure_code"], "expected_paths_mismatch")
        self.assertEqual(metadata["retry_safe"], "false")
        state, action = self.state(metadata)
        self.assertEqual(git_delivery_failure_evidence(state, action), self.details())

    def test_malformed_or_ambiguous_evidence_fails_closed(self):
        metadata = git_delivery_failure_metadata(
            GitOwnershipError("safe", details=self.details()), "ownership"
        )
        metadata["expected_paths"] = "not-json"
        state, action = self.state(metadata)
        self.assertIsNone(git_delivery_failure_evidence(state, action))

        valid = git_delivery_failure_metadata(
            GitOwnershipError("safe", details=self.details()), "ownership"
        )
        state, action = self.state(valid)
        duplicate = replace(state.events[-1], id="event-git-2")
        self.assertIsNone(
            git_delivery_failure_evidence(
                replace(state, events=state.events + (duplicate,)), action
            )
        )

    def test_diagnostic_paths_and_summaries_are_bounded(self):
        with self.assertRaises(ValueError):
            replace(self.details(), actual_paths=("bad\npath.py",))
        with self.assertRaises(ValueError):
            replace(
                self.details(),
                expected_paths=tuple(f"path-{index}.py" for index in range(101)),
            )
        with self.assertRaises(ValueError):
            replace(self.details(), safe_summary="x" * 201)


if __name__ == "__main__":
    unittest.main()
