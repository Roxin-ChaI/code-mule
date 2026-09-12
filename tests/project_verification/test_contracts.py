import unittest
from dataclasses import replace

from code_mule.project_verification import (
    FinalReviewDecision,
    ProjectVerificationCategory,
    ProjectVerificationCheck,
    ProjectVerificationCommand,
    ProjectVerificationResult,
    ProjectVerificationSpec,
    ProjectVerificationStatus,
)
from code_mule.state.serialization import (
    CURRENT_SCHEMA_VERSION,
    deserialize_project_state,
    serialize_project_state,
)

from state import CREATED, UPDATED, make_project_state


def evidence():
    command = ProjectVerificationCommand(
        "unit tests",
        ProjectVerificationCategory.TEST,
        ("python", "-m", "unittest"),
        timeout_seconds=30,
    )
    spec = ProjectVerificationSpec("project-1", (command,))
    check = ProjectVerificationCheck(
        command.name,
        command.category,
        command.command,
        ProjectVerificationStatus.PASS,
        0,
        "Required check passed.",
        True,
    )
    git_check = ProjectVerificationCheck(
        "Git clean",
        ProjectVerificationCategory.GIT_CLEAN,
        ("git", "status", "--short"),
        ProjectVerificationStatus.PASS,
        0,
        "Repository is clean at the expected HEAD.",
        True,
    )
    result = ProjectVerificationResult(
        "verify-1", "project-1", "plan-1", "a" * 40, "a" * 40,
        (check, git_check), CREATED, UPDATED,
        FinalReviewDecision.APPROVE, "Final delivery approved.",
    )
    return spec, result


class ProjectVerificationContractTests(unittest.TestCase):
    def test_typed_spec_result_and_pass_rule(self):
        spec, result = evidence()
        self.assertEqual(spec.commands[0].command, ("python", "-m", "unittest"))
        self.assertTrue(result.passed)
        failed = replace(
            result.checks[0],
            status=ProjectVerificationStatus.FAIL,
            exit_code=1,
            safe_summary="Required check failed.",
        )
        self.assertFalse(replace(result, checks=(failed, result.checks[1])).passed)

    def test_contract_rejects_shellless_and_invalid_results(self):
        with self.assertRaises(ValueError):
            ProjectVerificationCommand(
                "bad", ProjectVerificationCategory.TEST, (), timeout_seconds=1
            )
        with self.assertRaises(ValueError):
            ProjectVerificationCheck(
                "bad", ProjectVerificationCategory.TEST, ("test",),
                ProjectVerificationStatus.PASS, None, "bad", True,
            )

    def test_schema_v8_round_trip_and_v7_migration(self):
        spec, result = evidence()
        source = make_project_state()
        state = replace(
            source,
            project=replace(source.project, objective="Build persistence"),
            project_verification_spec=spec,
            project_verification_results=(result,),
        )
        payload = serialize_project_state(state)
        self.assertEqual(payload["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(deserialize_project_state(payload), state)

        legacy = serialize_project_state(make_project_state())
        legacy["schema_version"] = 7
        for report in legacy["execution_reports"]:
            report["human_action_required"] = report.pop("human_action") is not None
        legacy_project = dict(legacy["project"])
        legacy_project.pop("objective")
        legacy["project"] = legacy_project
        legacy.pop("project_verification_spec")
        legacy.pop("project_verification_results")
        migrated = deserialize_project_state(legacy)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 15)
        self.assertIsNone(migrated.project.objective)
        self.assertIsNone(migrated.project_verification_spec)
        self.assertEqual(migrated.project_verification_results, ())
