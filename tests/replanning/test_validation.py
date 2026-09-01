from dataclasses import replace
from datetime import UTC, datetime
import unittest

from code_mule.domain.enums import (
    ChangeRequestStatus,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    TaskStatus,
)
from code_mule.domain.models import (
    ChangeRequest,
    Milestone,
    Plan,
    Project,
    Requirement,
    Task,
)
from code_mule.replanning import (
    ChangeReplanValidator,
    ConflictingTaskChange,
    InvalidReplanDependency,
    InvalidReplanProposal,
    ReplanDependencyCycle,
    ReplanIdCollision,
    UnknownReplanReference,
)
from code_mule.state.models import ProjectState
from code_mule.supervisor import (
    ImpactAnalysisResult,
    MilestoneProposal,
    RequirementProposal,
    RequirementUpdateProposal,
    TaskDependencyChange,
    TaskProposal,
)


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def requirement(requirement_id: str = "REQ-BASE") -> Requirement:
    return Requirement(
        requirement_id,
        "project-1",
        "Calculator",
        "Support calculator operations.",
        RequirementStatus.ACTIVE,
        "high",
        ("operations are tested",),
        "boss",
        NOW,
        NOW,
    )


def task(
    task_id: str,
    *,
    status: TaskStatus,
    dependencies: tuple[str, ...] = (),
) -> Task:
    return Task(
        task_id,
        "M1",
        task_id,
        f"Implement {task_id}",
        status,
        dependencies,
        (f"{task_id} verified",),
        1 if status is TaskStatus.COMPLETED else 0,
        NOW,
        NOW,
        ("REQ-BASE",),
    )


def change_request() -> ChangeRequest:
    return ChangeRequest(
        "CHANGE-1",
        "project-1",
        "Add multiply support and tests.",
        ChangeRequestStatus.PENDING,
        (),
        "boss",
        NOW,
    )


def state() -> ProjectState:
    return ProjectState(
        project=Project(
            "project-1",
            "Calculator",
            ProjectStatus.CHANGE_REQUESTED,
            "PLAN-1",
            None,
            NOW,
            NOW,
        ),
        requirements=(requirement(),),
        plans=(
            Plan(
                "PLAN-1",
                "project-1",
                1,
                PlanStatus.ACTIVE,
                ("REQ-BASE",),
                ("M1",),
                NOW,
            ),
        ),
        milestones=(Milestone("M1", "PLAN-1", "Base", "active", ("T1", "T2")),),
        tasks=(
            task("T1", status=TaskStatus.COMPLETED),
            task("T2", status=TaskStatus.PENDING, dependencies=("T1",)),
        ),
        change_requests=(change_request(),),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


def valid_proposal() -> ImpactAnalysisResult:
    return ImpactAnalysisResult(
        change_request_id="CHANGE-1",
        summary="Add multiply without discarding completed work.",
        architecture_impact="One calculator operation and tests.",
        affected_components=("calculator",),
        affected_requirement_ids=(),
        affected_task_ids=(),
        affected_completed_tasks=(),
        affected_in_progress_tasks=(),
        affected_pending_tasks=(),
        requirements_to_add=(
            RequirementProposal(
                "REQ-MULTIPLY",
                "Multiply",
                "Support multiplication.",
                "high",
                ("multiply is tested",),
            ),
        ),
        requirements_to_update=(),
        tasks_to_add=(
            TaskProposal(
                "T3",
                "Add multiply",
                "Implement and test multiplication.",
                ("T1",),
                ("multiply tests pass",),
                ("REQ-MULTIPLY",),
            ),
        ),
        tasks_to_reopen=(),
        tasks_to_cancel=(),
        milestone_ids_reused=(),
        milestones=(
            MilestoneProposal("M2", "Changed calculator", ("T1", "T2", "T3")),
        ),
        dependency_changes=(),
        risks=(),
        recommendation="Create Plan v2.",
        rationale="The change is additive.",
    )


class ChangeReplanValidatorTests(unittest.TestCase):
    def setUp(self):
        self.state = state()
        self.change = self.state.change_requests[0]
        self.validator = ChangeReplanValidator()

    def test_valid_addition_preserves_completed_task_and_traceability(self):
        self.validator.validate(self.state, self.change, valid_proposal())

    def test_existing_milestone_is_reused_by_reference(self):
        proposal = replace(
            valid_proposal(),
            milestone_ids_reused=("M1",),
            milestones=(MilestoneProposal("M2", "Multiply", ("T3",)),),
        )
        self.validator.validate(self.state, self.change, proposal)

    def test_historical_milestone_id_cannot_be_redeclared_as_new(self):
        proposal = replace(
            valid_proposal(),
            milestones=(
                MilestoneProposal("M1", "Redeclared", ("T1", "T2", "T3")),
            ),
        )
        with self.assertRaises(ReplanIdCollision):
            self.validator.validate(self.state, self.change, proposal)

    def test_completed_task_can_only_change_through_explicit_reopen(self):
        proposal = replace(
            valid_proposal(),
            affected_task_ids=("T1",),
            affected_completed_tasks=("T1",),
            tasks_to_reopen=("T1",),
        )
        self.validator.validate(self.state, self.change, proposal)

        not_completed = replace(proposal, tasks_to_reopen=("T2",))
        with self.assertRaises(ConflictingTaskChange):
            self.validator.validate(self.state, self.change, not_completed)

    def test_requirement_update_uses_new_id_and_explicit_supersedes(self):
        proposal = replace(
            valid_proposal(),
            affected_requirement_ids=("REQ-BASE",),
            affected_task_ids=("T2",),
            affected_pending_tasks=("T2",),
            requirements_to_add=(),
            requirements_to_update=(
                RequirementUpdateProposal(
                    "REQ-BASE",
                    RequirementProposal(
                        "REQ-BASE-V2",
                        "Calculator v2",
                        "Calculator plus multiply.",
                        "high",
                        ("all operations are tested",),
                    ),
                ),
            ),
            tasks_to_add=(
                replace(
                    valid_proposal().tasks_to_add[0],
                    requirement_ids=("REQ-BASE-V2",),
                ),
            ),
            milestones=(MilestoneProposal("M2", "Changed", ("T3",)),),
            tasks_to_cancel=("T2",),
        )
        with self.assertRaises(InvalidReplanProposal):
            self.validator.validate(self.state, self.change, proposal)

        traced = replace(
            proposal,
            milestones=(MilestoneProposal("M2", "Changed", ("T1", "T3")),),
        )
        self.validator.validate(self.state, self.change, traced)

    def test_unknown_affected_id_and_new_id_collision_are_rejected(self):
        unknown = replace(valid_proposal(), affected_task_ids=("Task title",))
        with self.assertRaises(UnknownReplanReference):
            self.validator.validate(self.state, self.change, unknown)

        collisions = (
            replace(
                valid_proposal(),
                tasks_to_add=(
                    replace(valid_proposal().tasks_to_add[0], id="T2"),
                ),
            ),
            replace(
                valid_proposal(),
                requirements_to_add=(
                    replace(
                        valid_proposal().requirements_to_add[0], id="REQ-BASE"
                    ),
                ),
            ),
        )
        for collision in collisions:
            with self.subTest(collision=collision):
                with self.assertRaises(ReplanIdCollision):
                    self.validator.validate(self.state, self.change, collision)

    def test_conflicting_reopen_cancel_is_rejected(self):
        proposal = replace(
            valid_proposal(),
            affected_task_ids=("T1",),
            affected_completed_tasks=("T1",),
            tasks_to_reopen=("T1",),
            tasks_to_cancel=("T1",),
        )
        with self.assertRaises(ConflictingTaskChange):
            self.validator.validate(self.state, self.change, proposal)

    def test_cancelled_task_cannot_satisfy_dependency(self):
        proposal = replace(
            valid_proposal(),
            affected_task_ids=("T2",),
            affected_pending_tasks=("T2",),
            tasks_to_cancel=("T2",),
            tasks_to_add=(
                replace(valid_proposal().tasks_to_add[0], dependencies=("T2",)),
            ),
            milestones=(MilestoneProposal("M2", "Changed", ("T1", "T3")),),
        )
        with self.assertRaises(InvalidReplanDependency):
            self.validator.validate(self.state, self.change, proposal)

    def test_invalid_dependency_and_cycle_are_rejected(self):
        invalid = replace(
            valid_proposal(),
            tasks_to_add=(
                replace(valid_proposal().tasks_to_add[0], dependencies=("missing",)),
            ),
        )
        with self.assertRaises(InvalidReplanDependency):
            self.validator.validate(self.state, self.change, invalid)

        cyclic = replace(
            valid_proposal(),
            affected_task_ids=("T2",),
            affected_pending_tasks=("T2",),
            tasks_to_add=(
                replace(valid_proposal().tasks_to_add[0], dependencies=("T2",)),
            ),
            dependency_changes=(TaskDependencyChange("T2", ("T3",)),),
        )
        with self.assertRaises(ReplanDependencyCycle):
            self.validator.validate(self.state, self.change, cyclic)

    def test_membership_and_requirement_coverage_are_fail_closed(self):
        duplicate_membership = replace(
            valid_proposal(),
            milestones=(
                MilestoneProposal("M2", "One", ("T1", "T2", "T3")),
                MilestoneProposal("M3", "Two", ("T3",)),
            ),
        )
        with self.assertRaises(InvalidReplanProposal):
            self.validator.validate(self.state, self.change, duplicate_membership)

        uncovered = replace(
            valid_proposal(),
            tasks_to_add=(
                replace(
                    valid_proposal().tasks_to_add[0],
                    requirement_ids=("REQ-BASE",),
                ),
            ),
        )
        with self.assertRaises(InvalidReplanProposal):
            self.validator.validate(self.state, self.change, uncovered)


if __name__ == "__main__":
    unittest.main()
