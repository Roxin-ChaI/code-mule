"""Manual, billable DeepSeek Supervisor E2E. Never run in automation."""

from datetime import datetime, timezone
import os
from pathlib import Path
import sys


_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from openai import DefaultHttpx2Client, OpenAI  # noqa: E402

from code_mule.domain.enums import ProjectStatus  # noqa: E402
from code_mule.domain.models import Project  # noqa: E402
from code_mule.state.models import ProjectState  # noqa: E402
from code_mule.supervisor.contracts import ProgressReportRequest  # noqa: E402
from code_mule.supervisor.providers.deepseek import (  # noqa: E402
    DeepSeekSupervisorConfig,
    DeepSeekSupervisorModelClient,
)
from code_mule.supervisor.service import SupervisorService  # noqa: E402


def _build_compatibility_client(deepseek_api_key: str) -> OpenAI:
    return OpenAI(
        api_key=deepseek_api_key,
        base_url="https://api.deepseek.com",
        max_retries=0,
        http_client=DefaultHttpx2Client(trust_env=False),
    )


def _build_supervisor_config(model: str) -> DeepSeekSupervisorConfig:
    return DeepSeekSupervisorConfig(model=model, max_output_tokens=None)


def _minimal_project_state() -> ProjectState:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ProjectState(
        project=Project(
            id="manual-e2e-project",
            name="Code Mule Manual DeepSeek E2E",
            status=ProjectStatus.IDLE,
            active_plan_id=None,
            current_task_id=None,
            created_at=timestamp,
            updated_at=timestamp,
        ),
        requirements=(),
        plans=(),
        milestones=(),
        tasks=(),
        change_requests=(),
        impact_analyses=(),
        decisions=(),
        execution_reports=(),
        quality_status=None,
        events=(),
    )


def main() -> int:
    print("REAL DEEPSEEK API CALL — MANUAL ONLY")
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    model = os.getenv("CODE_MULE_DEEPSEEK_MODEL")
    if not deepseek_api_key:
        print("ERROR: DEEPSEEK_API_KEY is required.", file=sys.stderr)
        return 2
    if not model:
        print("ERROR: CODE_MULE_DEEPSEEK_MODEL is required.", file=sys.stderr)
        return 2

    compatibility_client = _build_compatibility_client(deepseek_api_key)
    provider = DeepSeekSupervisorModelClient(
        compatibility_client,
        _build_supervisor_config(model),
    )
    supervisor = SupervisorService(provider)
    report = supervisor.report_progress(
        ProgressReportRequest(
            project_state=_minimal_project_state(),
            question=None,
        )
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
