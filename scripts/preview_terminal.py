"""Deterministic in-memory terminal preview. No repository/model/state writes."""

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import sys

from code_mule.domain import Project, ProjectStatus
from code_mule.state.models import ProjectState
from code_mule.presentation import render_project, render_project_diagnosis
from code_mule.presentation.terminal import TerminalDashboard
from code_mule.diagnosis import ProjectDiagnosisService
from code_mule.recovery import SafePoint, SafePointKind


def preview_state():
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return ProjectState(
        project=Project("demo-project", "My Project", ProjectStatus.IDLE, None, None, timestamp, timestamp),
        requirements=(), plans=(), milestones=(), tasks=(), change_requests=(),
        impact_analyses=(), decisions=(), execution_reports=(), quality_status=None, events=(),
        latest_safe_point=SafePoint(SafePointKind.PROJECT_IDLE, timestamp),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, choices=(40, 60, 80, 120))
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--plain", action="store_true")
    arguments = parser.parse_args()
    terminal = TerminalDashboard.for_stream(sys.stdout, width=arguments.width)
    if arguments.plain:
        terminal = replace(terminal, interactive=False)
    state = preview_state()
    lines = (render_project_diagnosis(ProjectDiagnosisService().diagnose(state), terminal=terminal)
             if arguments.diagnose else render_project(state, terminal=terminal))
    print("DEMO ONLY - in-memory facts; no model calls or state/workspace changes.")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
