"""Manual visual acceptance for the persistent terminal UI.

Runs the real terminal UI against a synthetic local project.  It never calls a
model, a Worker, the network, or a shell.

    .venv/bin/python scripts/tui_demo.py

Keys: type a Boss command and press Enter (try `status`, `launch`, `inspect`,
`resolve action-demo --strategy acknowledge`); Up/Down/PgUp/PgDn scroll the
activity pane; Ctrl+L repaints; Ctrl+C exits without touching project state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_mule.tui.activity import ActivityKind  # noqa: E402
from code_mule.tui.app import availability, fallback_lines, run_terminal  # noqa: E402
from code_mule.tui.controller import TerminalController  # noqa: E402
from code_mule.tui.demo import demo_commands, demo_state  # noqa: E402

SEED_ACTIVITY = (
    (ActivityKind.TASK, "T1 Add sandbox-safe assertions for server.py — completed"),
    (ActivityKind.COMMAND, "python3 -m unittest discover -s tests -v — pass (8 tests)"),
    (ActivityKind.TASK, "T2 Author code-mule-delivery.json candidate — completed"),
    (ActivityKind.RUNTIME, "Final Verification: delivery manifest verified (service)"),
    (ActivityKind.HUMAN, "Human Gate opened: Worker needs a Boss decision"),
)


def main() -> int:
    controller = TerminalController(
        demo_commands(),
        demo_state,
        input_stream=sys.stdin,
    )
    now = datetime.now(UTC)
    for kind, text in SEED_ACTIVITY:
        controller.activity.append(now, kind, text)
    controller.activity.append(
        now, ActivityKind.INFO, "demo project loaded; no model or network call was made"
    )
    verdict = availability(sys.stdin, sys.stdout)
    if not verdict.interactive:
        for line in fallback_lines(controller):
            print(line)
        print(f"(persistent UI unavailable: {verdict.reason})")
        print("Run this script directly in a terminal for the full UI.")
        return 0
    print("Starting the demo UI. Ctrl+C exits without touching project state.")
    return run_terminal(controller, stdin=sys.stdin, stdout=sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
