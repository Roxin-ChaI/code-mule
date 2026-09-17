"""Synthetic local project for manual visual acceptance of the terminal UI.

Nothing here calls a model, the network, or a Worker: it exists so the Boss can
look at the fixed layout, scrolling, and HumanAction rendering by hand.
"""

from datetime import UTC, datetime

from code_mule.cli.contracts import CliCommandResult, CliExitCode
from code_mule.domain.enums import (
    HumanActionCategory,
    HumanActionStatus,
    PlanStatus,
    ProjectStatus,
    RequirementStatus,
    TaskStatus,
)
from code_mule.domain.models import (
    HumanAction,
    Milestone,
    Plan,
    Project,
    Requirement,
    Task,
)
from code_mule.state.models import ProjectState

from .activity import ActivityKind


NOW = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)


def demo_state() -> ProjectState:
    """A small, valid project snapshot shaped like a real one."""

    return ProjectState(
        project=Project(
            "demo-project",
            "Demo Project",
            ProjectStatus.HUMAN_REQUIRED,
            "plan-demo",
            "task-2",
            NOW,
            NOW,
        ),
        requirements=(
            Requirement(
                "req-1", "demo-project", "Service", "Expose a local service",
                RequirementStatus.ACTIVE, "high", ("banner", "health"),
                "boss", NOW, NOW,
            ),
        ),
        plans=(
            Plan(
                "plan-demo", "demo-project", 1, PlanStatus.ACTIVE,
                ("req-1",), ("milestone-1",), NOW,
            ),
        ),
        milestones=(
            Milestone(
                "milestone-1", "plan-demo", "Handoff", "active",
                ("task-1", "task-2"),
            ),
        ),
        tasks=(
            Task(
                "task-1", "milestone-1", "Add sandbox-safe tests",
                "Pure-function assertions only", TaskStatus.COMPLETED,
                (), ("no bind",), 1, NOW, NOW, ("req-1",),
            ),
            Task(
                "task-2", "milestone-1", "Author delivery manifest",
                "Declare the service handoff", TaskStatus.IN_PROGRESS,
                ("task-1",), ("service type",), 0, NOW, NOW, ("req-1",),
            ),
        ),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
        human_actions=(
            HumanAction(
                "action-demo",
                "demo-project",
                "task-2",
                HumanActionCategory.WORKER_INPUT,
                "Worker needs a Boss decision",
                "Confirm the manifest entry point before delivery",
                "Delivery stays paused until the Boss decides",
                HumanActionStatus.PENDING,
                NOW,
            ),
        ),
    )


class DemoCommands:
    """Stand-in command layer: deterministic text, never a model call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _echo(self, name: str, *lines: str) -> CliCommandResult:
        self.calls.append(name)
        return CliCommandResult(CliExitCode.SUCCESS, lines or (f"{name}: demo output",))

    def status(self, verbose: bool = False) -> CliCommandResult:
        state = demo_state()
        return self._echo(
            "status",
            "DEMO PROJECT",
            f"Status      {state.project.status.value}",
            "Progress    1 / 2",
        )

    def launch(self, verbose: bool = False) -> CliCommandResult:
        return self._echo(
            "launch",
            "DEMO LAUNCH (no process was started)",
            "Access      http://127.0.0.1:49441/",
        )

    def app_status(self, verbose: bool = False) -> CliCommandResult:
        return self._echo("app-status", "DEMO RUNTIME  running / healthy")

    def stop_app(self, verbose: bool = False) -> CliCommandResult:
        return self._echo("stop-app", "DEMO STOP (no process was signalled)")

    def inspect(self, verbose: bool) -> CliCommandResult:
        return self._echo("inspect", "DEMO HUMAN ACTION action-demo")

    def resolve(self, action_id: str, strategy, verbose: bool = False) -> CliCommandResult:
        return self._echo("resolve", f"DEMO RESOLVE {action_id} via {strategy.value}")

    def approve(self, action_id: str, verbose: bool = False) -> CliCommandResult:
        return self._echo("approve", f"DEMO APPROVE {action_id}")

    def reject(self, action_id: str, verbose: bool = False) -> CliCommandResult:
        return self._echo("reject", f"DEMO REJECT {action_id}")

    def answer(self, action_id: str, answer: str, verbose: bool = False) -> CliCommandResult:
        return self._echo("answer", f"DEMO ANSWER {action_id}: {answer}")

    def change(self, request: str, verbose: bool = False) -> CliCommandResult:
        return self._echo("change", f"DEMO CHANGE: {request}")

    def __getattr__(self, name: str):
        def _unsupported(*args, **kwargs) -> CliCommandResult:
            self.calls.append(name)
            return CliCommandResult(
                CliExitCode.SUCCESS,
                (f"demo: {name} is not simulated; no model was called.",),
            )

        return _unsupported


def demo_commands() -> DemoCommands:
    return DemoCommands()


DEMO_ACTIVITY_COUNT = 60


def seed_demo_activity(controller, *, count: int = DEMO_ACTIVITY_COUNT) -> int:
    """Fill the Activity pane with numbered events so scrolling is obvious.

    Numbered on purpose: the Boss can see exactly which window is on screen and
    confirm that Up/Down, PageUp/PageDown, and End moved it.
    """

    now = datetime.now(UTC)
    kinds = (
        ActivityKind.TASK,
        ActivityKind.COMMAND,
        ActivityKind.RESPONSE,
        ActivityKind.RUNTIME,
    )
    for index in range(1, count + 1):
        controller.activity.append(
            now,
            kinds[(index - 1) % len(kinds)],
            f"event {index:03d} · demo activity line for scroll verification",
        )
    controller.activity.append(
        now, ActivityKind.INFO, "demo project loaded; no model or network call was made"
    )
    return count


__all__ = [
    "DEMO_ACTIVITY_COUNT",
    "DemoCommands",
    "demo_commands",
    "demo_state",
    "seed_demo_activity",
]
