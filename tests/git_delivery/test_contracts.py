import unittest
from dataclasses import replace

from code_mule.git_delivery import GitBaseline, GitChangeSet, GitCommitResult
from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    deserialize_project_state,
    serialize_project_state,
)

from tests.state import CREATED, make_project_state


def evidence():
    baseline = GitBaseline("task-1", "/workspace", "a" * 40, ())
    changes = GitChangeSet(
        "task-1", "/workspace", "a" * 40, ("src/add.py",), ("src/add.py",), ()
    )
    commit = GitCommitResult(
        "task-1",
        "/workspace",
        "a" * 40,
        "b" * 40,
        "feat(task): add support",
        ("src/add.py",),
        ("src/add.py",),
        CREATED,
    )
    return baseline, changes, commit


class GitDeliveryContractTests(unittest.TestCase):
    def test_contracts_are_typed_and_require_repository_relative_paths(self):
        baseline, changes, commit = evidence()
        self.assertEqual(baseline.status_entries, ())
        self.assertEqual(changes.untracked_paths, ("src/add.py",))
        self.assertEqual(commit.commit_sha, "b" * 40)
        with self.assertRaises(ValueError):
            replace(changes, changed_paths=("/tmp/outside",))
        with self.assertRaises(ValueError):
            replace(commit, staged_paths=())

    def test_schema_v8_round_trip_persists_git_evidence(self):
        baseline, changes, commit = evidence()
        state = replace(
            make_project_state(),
            git_baselines=(baseline,),
            git_change_sets=(changes,),
            git_commit_results=(commit,),
        )
        payload = serialize_project_state(state)
        self.assertEqual(payload["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(deserialize_project_state(payload), state)

    def test_schema_v6_migrates_with_empty_git_evidence(self):
        payload = serialize_project_state(make_project_state())
        payload["schema_version"] = 6
        for report in payload["execution_reports"]:
            report["human_action_required"] = report.pop("human_action") is not None
        payload.pop("git_baselines")
        payload.pop("git_change_sets")
        payload.pop("git_commit_results")
        state = deserialize_project_state(payload)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 15)
        self.assertEqual(state.git_baselines, ())
        self.assertEqual(state.git_change_sets, ())
        self.assertEqual(state.git_commit_results, ())
