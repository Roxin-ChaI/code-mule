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

from code_mule.tui.app import availability, fallback_lines, run_terminal  # noqa: E402
from code_mule.tui.controller import TerminalController  # noqa: E402
from code_mule.tui.demo import demo_commands, demo_state, seed_demo_activity  # noqa: E402


def main() -> int:
    controller = TerminalController(
        demo_commands(),
        demo_state,
        input_stream=sys.stdin,
    )
    # Demo/debug display: a safe parsed key name plus 60 numbered events, so
    # scrolling, auto-follow, and every shortcut are directly observable.
    controller.state.key_debug = True
    seed_demo_activity(controller)
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
