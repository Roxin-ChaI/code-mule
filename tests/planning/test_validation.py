from dataclasses import replace
from datetime import UTC, datetime
import unittest

from code_mule.domain.enums import (
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
)
from code_mule.domain.models import (
    ChangeRequest,
    Milestone,
    Plan,
    Project,
    Requirement,
    Task,
)
from code_mule.planning import (
    DuplicateProposalId,
    InvalidPlanProposal,
    InvalidProposalDependency,
    PlanProposalValidator,
    ProposalDependencyCycle,
    UnknownProposalReference,
)
from code_mule.state.models import ProjectState
from code_mule.supervisor import (
    MilestoneProposal,
    PlanProposal,
    RequirementProposal,
    TaskProposal,
)


NOW = datetime(2026, 9, 1, tzinfo=UTC)


def empty_state(
    *,
    status: ProjectStatus = ProjectStatus.IDLE,
    active_plan_id: str | None = None,
    current_task_id: str | None = None,
    requirements: tuple[Requirement, ...] = (),
    plans: tuple[Plan, ...] = (),
    milestones: tuple[Milestone, ...] = (),
    tasks: tuple[Task, ...] = (),
    change_requests: tuple[ChangeRequest, ...] = (),
) -> ProjectState:
    return ProjectState(
        project=Project(
            "project-1",
            "Tiny Calculator",
            status,
            active_plan_id,
            current_task_id,
            NOW,
            NOW,
        ),
        requirements=requirements,
        plans=plans,
        milestones=milestones,
        tasks=tasks,
        change_requests=change_requests,
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


def requirement_proposal(requirement_id: str = "REQ-001") -> RequirementProposal:
    return RequirementProposal(
        requirement_id,
        "Calculator operations",
        "Provide verified arithmetic operations.",
        "high",
        ("add and subtract are verified",),
    )


def task_proposal(
    task_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    requirement_ids: tuple[str, ...] = ("REQ-001",),
) -> TaskProposal:
    return TaskProposal(
        task_id,
        f"Task {task_id}",
        f"Implement {task_id}",
        dependencies,
        (f"{task_id} is verified",),
        requirement_ids,
    )


def valid_proposal() -> PlanProposal:
    return PlanProposal(
        summary="Build and test a tiny calculator",
        requirements=(requirement_proposal(),),
        requirements_considered=(),
        milestones=(
            MilestoneProposal("M1", "Calculator", ("T1", "T2")),
        ),
        tasks=(task_proposal("T1"), task_proposal("T2", dependencies=("T1",))),
        risks=(),
        rationale="Two small reviewable tasks",
    )


class PlanProposalValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = PlanProposalValidator()
        self.state = empty_state(status=ProjectStatus.PLANNING)

    def test_valid_proposal_and_existing_requirement_support(self):
        self.validator.validate(self.state, valid_proposal())
        existing = Requirement(
            "REQ-EXISTING",
            "project-1",
            "Existing",
            "Boss supplied",
            RequirementStatus.ACTIVE,
            "high",
            ("covered",),
            "boss",
            NOW,
            NOW,
        )
        proposal = replace(
            valid_proposal(),
            requirements_considered=(existing.id,),
            tasks=(
                replace(
                    valid_proposal().tasks[0],
                    requirement_ids=(existing.id, "REQ-001"),
                ),
                valid_proposal().tasks[1],
            ),
        )
        self.validator.validate(
            replace(self.state, requirements=(existing,)), proposal
        )

    def test_empty_plan_collections_fail_closed(self):
        proposal = valid_proposal()
        for field in ("requirements", "milestones", "tasks"):
            with self.subTest(field=field):
                with self.assertRaises(InvalidPlanProposal):
                    self.validator.validate(
                        self.state, replace(proposal, **{field: ()})
                    )

    def test_duplicate_and_cross_entity_ids_are_rejected(self):
        proposal = valid_proposal()
        cases = (
            replace(
                proposal,
                requirements=(requirement_proposal(), requirement_proposal()),
            ),
            replace(
                proposal,
                milestones=(proposal.milestones[0], proposal.milestones[0]),
            ),
            replace(proposal, tasks=(proposal.tasks[0], proposal.tasks[0])),
            replace(
                proposal,
                milestones=(MilestoneProposal("REQ-001", "Collision", ("T1", "T2")),),
            ),
        )
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(DuplicateProposalId):
                    self.validator.validate(self.state, invalid)

    def test_existing_entity_id_collision_is_rejected(self):
        historical = Plan(
            "REQ-001", "project-1", 1, PlanStatus.SUPERSEDED, (), (), NOW
        )
        with self.assertRaises(InvalidPlanProposal):
            self.validator.validate(replace(self.state, plans=(historical,)), valid_proposal())

    def test_milestone_membership_is_complete_unique_and_known(self):
        proposal = valid_proposal()
        cases = (
            replace(
                proposal,
                milestones=(MilestoneProposal("M1", "Empty", ()),),
            ),
            replace(
                proposal,
                milestones=(MilestoneProposal("M1", "Unknown", ("T1", "T3")),),
            ),
            replace(
                proposal,
                milestones=(
                    MilestoneProposal("M1", "One", ("T1", "T2")),
                    MilestoneProposal("M2", "Two", ("T2",)),
                ),
            ),
            replace(
                proposal,
                milestones=(MilestoneProposal("M1", "Orphan", ("T1",)),),
            ),
        )
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidPlanProposal):
                    self.validator.validate(self.state, invalid)

    def test_requirement_references_and_coverage_are_enforced(self):
        proposal = valid_proposal()
        unknown = replace(
            proposal,
            tasks=(
                replace(proposal.tasks[0], requirement_ids=("UNKNOWN",)),
                proposal.tasks[1],
            ),
        )
        with self.assertRaises(UnknownProposalReference):
            self.validator.validate(self.state, unknown)

        uncovered = replace(
            proposal,
            requirements=(requirement_proposal(), requirement_proposal("REQ-002")),
        )
        with self.assertRaises(InvalidPlanProposal):
            self.validator.validate(self.state, uncovered)

        with self.assertRaises(ValueError):
            replace(proposal.tasks[0], requirement_ids=())
        with self.assertRaises(ValueError):
            replace(proposal.tasks[0], acceptance_criteria=())

    def test_dependency_references_self_duplicates_and_cycles_are_rejected(self):
        proposal = valid_proposal()
        cases = (
            (
                replace(
                    proposal,
                    tasks=(proposal.tasks[0], replace(proposal.tasks[1], dependencies=("T3",))),
                ),
                InvalidProposalDependency,
            ),
            (
                replace(
                    proposal,
                    tasks=(replace(proposal.tasks[0], dependencies=("T1",)), proposal.tasks[1]),
                ),
                InvalidProposalDependency,
            ),
            (
                replace(
                    proposal,
                    tasks=(proposal.tasks[0], replace(proposal.tasks[1], dependencies=("T1", "T1"))),
                ),
                DuplicateProposalId,
            ),
            (
                replace(
                    proposal,
                    tasks=(replace(proposal.tasks[0], dependencies=("T2",)), proposal.tasks[1]),
                ),
                ProposalDependencyCycle,
            ),
        )
        for invalid, error in cases:
            with self.subTest(error=error):
                with self.assertRaises(error):
                    self.validator.validate(self.state, invalid)


if __name__ == "__main__":
    unittest.main()
