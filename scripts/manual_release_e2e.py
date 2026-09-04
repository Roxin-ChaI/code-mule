"""Boss-only authenticated DeepSeek + real Codex release E2E.

This script creates a disposable repository and makes real paid model calls.
Automated verification must compile this file but must never execute it.
"""

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from openai import DefaultHttpx2Client, OpenAI  # noqa: E402

from code_mule.supervisor.providers.deepseek import (  # noqa: E402
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService  # noqa: E402
from code_mule.progress import ConsoleProgressRenderer  # noqa: E402
from scripts.local_release_readiness_e2e import (  # noqa: E402
    DEFAULT_RELEASE_WORKER_TIMEOUT_SECONDS,
    ReleaseScenarioInterrupted,
    run_release_scenario,
)


OBJECTIVE = (
    "Create a small Python calculator with add and subtract functions and unittest "
    "coverage. Decompose the work into independently reviewable Tasks."
)
CHANGE = "Add multiply support and corresponding unittest coverage."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Boss-only authenticated release E2E in a disposable repository."
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=DEFAULT_RELEASE_WORKER_TIMEOUT_SECONDS,
        help="absolute deadline for each Codex turn (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | tuple[str, ...] = ()) -> int:
    arguments = build_parser().parse_args(argv)
    print("REAL DEEPSEEK + CODEX RELEASE E2E — BOSS MANUAL ONLY")
    print("This creates disposable local commits and makes paid DeepSeek requests.")
    print("It never pushes, tags, releases, deploys, or mutates remote infrastructure.")
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2
    model = os.environ.get("CODE_MULE_DEEPSEEK_MODEL", "deepseek-v4-flash")
    try:
        supervisor = SupervisorService(
            DeepSeekSupervisorModelClient(
                OpenAI(
                    api_key=api_key,
                    base_url="https://api.deepseek.com",
                    max_retries=0,
                    http_client=DefaultHttpx2Client(trust_env=False),
                ),
                DeepSeekSupervisorConfig(model=model, max_output_tokens=None),
            )
        )
        with ConsoleProgressRenderer(sys.stderr) as progress:
            result = run_release_scenario(
                real_worker=True,
                supervisor=supervisor,
                objective=OBJECTIVE,
                change=CHANGE,
                worker_timeout_seconds=arguments.worker_timeout_seconds,
                progress_sink=progress,
            )
    except ReleaseScenarioInterrupted as error:
        _print_interruption(error)
        return 130
    except KeyboardInterrupt:
        print("MANUAL E2E INTERRUPTED", file=sys.stderr)
        print("Stage: outside active Worker boundary", file=sys.stderr)
        print("Task: unknown", file=sys.stderr)
        print("Project state: UNKNOWN", file=sys.stderr)
        print("Recovery required: inspect before retry", file=sys.stderr)
        return 130
    print(json.dumps(result, sort_keys=True))
    valid = (
        result["safe_point_status"] == "change_requested"
        and result["replacement_version"] == 2
        and result["final_status"] == "done"
        and result["workspace_clean"]
        and result["verification_passed"]
        and result["final_review"] == "approve"
        and result["session_count"] == result["unique_session_count"]
        and result["delivery_commit_count"] == result["repository_commit_count"]
    )
    return 0 if valid else 1


def _print_interruption(error: ReleaseScenarioInterrupted) -> None:
    details = error.details
    print("MANUAL E2E INTERRUPTED", file=sys.stderr)
    print(f"Stage: {details.stage}", file=sys.stderr)
    print(f"Task: {details.task_id or 'none'}", file=sys.stderr)
    print(f"Project state: {details.project_status.upper()}", file=sys.stderr)
    print(
        f"Recovery required: {'yes' if details.recovery_required else 'no'}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
