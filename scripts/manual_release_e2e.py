"""Boss-only authenticated DeepSeek + real Codex release E2E.

This script creates a disposable repository and makes real paid model calls.
Automated verification must compile this file but must never execute it.
"""

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
from scripts.local_release_readiness_e2e import run_release_scenario  # noqa: E402


OBJECTIVE = (
    "Create a small Python calculator with add and subtract functions and unittest "
    "coverage. Decompose the work into independently reviewable Tasks."
)
CHANGE = "Add multiply support and corresponding unittest coverage."


def main() -> int:
    print("REAL DEEPSEEK + CODEX RELEASE E2E — BOSS MANUAL ONLY")
    print("This creates disposable local commits and makes paid DeepSeek requests.")
    print("It never pushes, tags, releases, deploys, or mutates remote infrastructure.")
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2
    model = os.environ.get("CODE_MULE_DEEPSEEK_MODEL", "deepseek-v4-flash")
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
    result = run_release_scenario(
        real_worker=True,
        supervisor=supervisor,
        objective=OBJECTIVE,
        change=CHANGE,
    )
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


if __name__ == "__main__":
    raise SystemExit(main())
