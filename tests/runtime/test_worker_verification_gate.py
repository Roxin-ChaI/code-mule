"""A completed report must not deliver without its required verification.

These cover the v3 finding: the Worker created a test file, never ran the
required check, and still claimed `status=completed`.
"""

from __future__ import annotations

import unittest

from code_mule.domain.enums import (
    HumanActionCategory,
    ProjectStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import ExecutionReport
from code_mule.domain.worker_verification import (
    WorkerCheckStatus,
    WorkerCheckType,
    WorkerVerificationCheck,
    blocking_check_metadata,
    blocking_checks,
    legacy_checks,
    report_verification_checks,
    unmet_check_summary,
    unmet_required_checks,
)
from code_mule.runtime import TaskPromptBuilder

from .test_cycle import (
    NOW,
    FakeStore,
    FakeSupervisor,
    build_cycle,
    cycle_state,
    review,
)


def check(name, status, *, required=True, kind=WorkerCheckType.TEST):
    return WorkerVerificationCheck(name, kind, status, required)


class ReportWorkerSession:
    """Return one exact ExecutionReport so the gate can be exercised directly."""

    def __init__(self, report: ExecutionReport):
        self.report = report
        self.started = 0
        self.closed = 0
        self.requests = []

    @property
    def thread_id(self):
        return "thread-1"

    @property
    def turn_id(self):
        return "turn-1"

    def start(self):
        self.started += 1

    def execute(self, request, *, report_id, created_at):
        self.requests.append(request)
        return self.report

    def close(self):
        self.closed += 1


def report_with(
    checks, *, task_id="TASK-1", status="completed", files=("tests/test_contract.py",)
):
    return ExecutionReport(
        id="report-1",
        task_id=task_id,
        attempt=1,
        status=status,
        files_changed=tuple(files),
        tests=tuple(f"{item.name}: {item.status.value}" for item in checks),
        static_checks=(),
        git_state="dirty",
        issues=(),
        human_action=None,
        summary="Added tests",
        created_at=NOW,
        verification_checks=tuple(checks),
    )


def gated_state(checks, **kwargs):
    store = FakeStore(cycle_state())
    task_id = store.current.tasks[0].id
    session = ReportWorkerSession(report_with(checks, task_id=task_id, **kwargs))
    supervisor = FakeSupervisor([review(SupervisorDecisionType.CONTINUE)])
    service, request, store, session, supervisor, _ = build_cycle(
        store=store, session=session, supervisor=supervisor, git_delivery=None
    )
    service.execute(request)
    return store.current, supervisor, session


class WorkerVerificationGateTests(unittest.TestCase):
    def test_required_check_executed_and_passing_reaches_review(self):
        state, supervisor, _session = gated_state(
            [
                check("python3 -m unittest discover -s tests", WorkerCheckStatus.PASS),
                check("read-only inspection", WorkerCheckStatus.PASS),
            ]
        )
        self.assertEqual(len(supervisor.requests), 1)
        self.assertNotEqual(state.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_required_check_not_executed_is_a_typed_failure(self):
        state, supervisor, _session = gated_state(
            [
                check("python3 -m unittest discover -s tests", WorkerCheckStatus.PASS),
                check("test_server_contract suite", WorkerCheckStatus.NOT_RUN),
            ]
        )
        self.assertIs(state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(supervisor.requests, [], "review must not run")
        action = state.human_actions[-1]
        self.assertIs(action.category, HumanActionCategory.WORKER_VERIFICATION)
        event = next(
            item for item in state.events if item.event_type == "task.verification_blocked"
        )
        self.assertEqual(event.metadata["check_status"], "not_run")
        self.assertEqual(event.metadata["check_required"], "true")
        self.assertEqual(event.metadata["unmet_check_count"], "1")
        self.assertEqual(
            event.metadata["unmet_check_1_name"], "test_server_contract suite"
        )

    def test_test_file_created_but_not_run_still_fails_with_the_check_named(self):
        state, _supervisor, session = gated_state(
            [
                check("test suite: python3 -m unittest discover -s tests", WorkerCheckStatus.NOT_RUN),
            ]
        )
        self.assertEqual(session.requests[0].task.id, "task-1")
        self.assertIs(state.project.status, ProjectStatus.HUMAN_REQUIRED)
        event = next(
            item for item in state.events if item.event_type == "task.verification_blocked"
        )
        self.assertEqual(event.metadata["check_status"], "not_run")
        # Command-like labels are withheld, but the check stays identifiable.
        self.assertIn(
            event.metadata["unmet_check_1_name"],
            {"[check name withheld]", "test suite: python3 -m unittest discover -s tests"},
        )
        self.assertEqual(state.git_commit_results, ())

    def test_optional_check_not_run_is_allowed(self):
        state, supervisor, _session = gated_state(
            [
                check("python3 -m unittest discover -s tests", WorkerCheckStatus.PASS),
                check("optional lint", WorkerCheckStatus.NOT_RUN, required=False),
            ]
        )
        self.assertEqual(len(supervisor.requests), 1)
        self.assertNotEqual(state.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_executed_and_matching_requirement_is_not_misjudged_as_not_run(self):
        executed = check(
            "python3 -B -m unittest tests.test_server_contract -v",
            WorkerCheckStatus.PASS,
        )
        self.assertEqual(unmet_required_checks((executed,)), ())
        self.assertEqual(blocking_checks((executed,)), ())
        state, supervisor, _session = gated_state([executed])
        self.assertEqual(len(supervisor.requests), 1)
        self.assertNotEqual(state.project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_optional_failure_still_blocks_delivery(self):
        state, supervisor, _session = gated_state(
            [
                check("python3 -m unittest discover -s tests", WorkerCheckStatus.PASS),
                check("optional lint", WorkerCheckStatus.FAIL, required=False),
            ]
        )
        self.assertIs(state.project.status, ProjectStatus.HUMAN_REQUIRED)
        self.assertEqual(supervisor.requests, [])

    def test_blocked_report_keeps_evidence_and_attempt_state(self):
        state, _supervisor, _session = gated_state(
            [check("required suite", WorkerCheckStatus.NOT_RUN)]
        )
        self.assertEqual(len(state.execution_reports), 1, "report stays as evidence")
        self.assertEqual(state.execution_reports[0].status, "completed")
        self.assertEqual(state.tasks[0].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(state.git_commit_results, ())

    def test_legacy_text_checks_are_adapted_and_still_gate(self):
        legacy = legacy_checks(("python -m unittest: not_run",), ())
        self.assertEqual(unmet_required_checks(legacy)[0].status, WorkerCheckStatus.NOT_RUN)
        report = report_with(())
        report.verification_checks = None
        report.tests = ("python -m unittest: not_run",)
        self.assertEqual(
            report_verification_checks(report)[0].status, WorkerCheckStatus.NOT_RUN
        )

    def test_bound_metadata_is_label_only_and_bounded(self):
        checks = tuple(
            check(f"check {index}", WorkerCheckStatus.NOT_RUN) for index in range(9)
        )
        metadata = blocking_check_metadata(checks)
        self.assertEqual(metadata["unmet_check_count"], "9")
        self.assertNotIn("unmet_check_9_name", metadata)
        self.assertEqual(metadata["unmet_check_5_name"], "check 4")
        summary = unmet_check_summary(checks)
        self.assertLessEqual(len(summary), 300)
        self.assertIn("+4 more", summary)


class WorkerPromptBoundaryTests(unittest.TestCase):
    """The prompt must state the sandbox-safety and Final Verification split."""

    def _prompt(self):
        state = cycle_state()
        return TaskPromptBuilder().build(state, state.tasks[0])

    def test_runtime_smoke_checks_are_excluded_from_required_worker_checks(self):
        prompt = self._prompt()
        self.assertIn("must not bind a socket", prompt)
        self.assertIn("Loopback launch, localhost health", prompt)
        self.assertIn("Final Verification", prompt)

    def test_prompt_requires_running_every_required_check_before_finishing(self):
        prompt = self._prompt()
        self.assertIn("Actually run every required check before you finish", prompt)
        self.assertIn("A required check that did not run or did not pass", prompt)
        self.assertIn("instead of claiming completion", prompt)


if __name__ == "__main__":
    unittest.main()
