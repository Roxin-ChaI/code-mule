"""Project-state persistence tests and fixtures."""

from datetime import datetime, timezone
from pathlib import Path
import sys


_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_mule.domain.enums import (  # noqa: E402
    ChangeRequestStatus,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    SupervisorDecisionType,
    TaskStatus,
)
from code_mule.domain.models import (  # noqa: E402
    ChangeRequest,
    Decision,
    ExecutionReport,
    ImpactAnalysis,
    Milestone,
    Plan,
    Project,
    ProjectEvent,
    QualityStatus,
    Requirement,
    Task,
)
from code_mule.state.models import ProjectState  # noqa: E402


CREATED = datetime(2026, 8, 30, 1, 2, 3, tzinfo=timezone.utc)
UPDATED = datetime(2026, 8, 31, 4, 5, 6, tzinfo=timezone.utc)


def make_project_state(
    *,
    name: str = "Code Mule",
    quality_status: QualityStatus | None = None,
) -> ProjectState:
    if quality_status is None:
        quality_status = QualityStatus("passed", "passed", "passed", "passed", True)
    return ProjectState(
        project=Project(
            "project-1",
            name,
            ProjectStatus.RUNNING,
            "plan-1",
            "task-1",
            CREATED,
            UPDATED,
        ),
        requirements=(
            Requirement(
                "req-2",
                "project-1",
                "Second",
                "Second requirement",
                RequirementStatus.ACTIVE,
                "medium",
                ("second-a", "second-b"),
                "boss",
                CREATED,
                UPDATED,
                "req-1",
            ),
            Requirement(
                "req-1",
                "project-1",
                "First",
                "First requirement",
                RequirementStatus.SUPERSEDED,
                "high",
                ("first",),
                "change-1",
                CREATED,
                UPDATED,
            ),
        ),
        plans=(
            Plan(
                "plan-1",
                "project-1",
                1,
                PlanStatus.ACTIVE,
                ("req-2", "req-1"),
                ("milestone-1",),
                CREATED,
            ),
        ),
        milestones=(
            Milestone(
                "milestone-1",
                "plan-1",
                "Persistence",
                "active",
                ("task-1",),
            ),
        ),
        tasks=(
            Task(
                "task-1",
                "milestone-1",
                "Serialize state",
                "Build strict serialization",
                TaskStatus.IN_PROGRESS,
                ("task-0",),
                ("round trip", "fail closed"),
                2,
                CREATED,
                UPDATED,
                ("req-2",),
            ),
        ),
        change_requests=(
            ChangeRequest(
                "change-1",
                "project-1",
                "Add persistence",
                ChangeRequestStatus.ANALYZING,
                ("req-1",),
                "boss",
                CREATED,
            ),
        ),
        impact_analyses=(
            ImpactAnalysis(
                "change-1",
                "local state boundary",
                ("state", "domain"),
                ("task-0",),
                ("task-1",),
                ("task-2",),
                ("task-3",),
                ("task-0",),
                ("task-old",),
                "create a new plan",
            ),
        ),
        decisions=(
            Decision(
                "decision-1",
                "task-1",
                SupervisorDecisionType.CONTINUE,
                "criteria satisfied",
                CREATED,
            ),
        ),
        execution_reports=(
            ExecutionReport(
                "report-1",
                "task-1",
                2,
                "passed",
                ("state/models.py", "state/serialization.py"),
                ("unittest: passed",),
                ("compileall: passed",),
                "clean",
                (),
                False,
                "state serialized",
                UPDATED,
            ),
        ),
        quality_status=quality_status,
        events=(
            ProjectEvent(
                "event-1",
                "project-1",
                "state.saved",
                "task-1",
                UPDATED,
                {"actor": "orchestrator", "result": "passed"},
            ),
        ),
    )


def make_project_state_without_quality() -> ProjectState:
    state = make_project_state()
    return ProjectState(
        project=state.project,
        requirements=state.requirements,
        plans=state.plans,
        milestones=state.milestones,
        tasks=state.tasks,
        change_requests=state.change_requests,
        impact_analyses=state.impact_analyses,
        decisions=state.decisions,
        execution_reports=state.execution_reports,
        quality_status=None,
        events=state.events,
    )
