"""Boss-only real DeepSeek routing smoke in a disposable persisted project."""

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for path in (_ROOT, _SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from code_mule.cli import main as cli_main  # noqa: E402
from code_mule.domain import (  # noqa: E402
    HumanActionCategory,
    Milestone,
    Plan,
    PlanStatus,
    ProjectStatus,
    Task,
    TaskStatus,
)
from code_mule.human import request_human_action  # noqa: E402
from code_mule.state.store import JsonProjectStateStore  # noqa: E402


def main() -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY is required.", file=sys.stderr)
        return 2
    with TemporaryDirectory(prefix="code-mule-boss-chat-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        state_file = root / "project-state.json"
        common = ["--state-file", str(state_file)]
        environment = {
            "DEEPSEEK_API_KEY": api_key,
            "CODE_MULE_DEEPSEEK_MODEL": os.environ.get(
                "CODE_MULE_DEEPSEEK_MODEL", "deepseek-v4-flash"
            ),
        }
        cli_main(
            [
                "init", "--project-id", "boss-chat", "--name", "Boss Chat",
                "--workspace", str(workspace), *common,
            ],
            environment=environment,
        )
        store = JsonProjectStateStore(state_file)
        state = store.load()
        now = datetime.now(UTC)
        plan = Plan("PLAN-1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), now)
        milestone = Milestone("M1", plan.id, "Foundation", "active", ("T1",))
        task = Task(
            "T1", "M1", "Build foundation", "Implement foundation",
            TaskStatus.PENDING, (), ("done",), 0, now, now,
        )
        store.save(
            replace(
                state,
                project=replace(
                    state.project,
                    status=ProjectStatus.RUNNING,
                    active_plan_id=plan.id,
                ),
                plans=(plan,), milestones=(milestone,), tasks=(task,),
            )
        )
        print("REAL DEEPSEEK BOSS CHAT — MANUAL ONLY")
        print("This may incur billing. No Worker or deployment is started.")
        first = cli_main(
            ["chat", *common],
            environment=environment,
            stdin=StringIO(
                "计划是什么？\n"
                "Could you explain what remains right now?\n"
                "先暂停\n继续\n增加 export CSV\n"
            ),
        )
        changed = store.load()
        human_base = replace(
            changed,
            project=replace(changed.project, status=ProjectStatus.RUNNING),
            change_requests=(),
        )
        ids = iter(("human-source", "human-requested"))
        store.save(
            request_human_action(
                human_base,
                category=HumanActionCategory.EXTERNAL_SIDE_EFFECT,
                summary="Push branch",
                requested_action="Push branch to GitHub",
                risk="Remote mutation",
                task_id="T1",
                operation_time=now,
                action_id="ACTION-1",
                event_id_factory=lambda: next(ids),
                source_event_types=("task.human_required",),
            )
        )
        second = cli_main(
            ["chat", *common],
            environment=environment,
            stdin=StringIO("发生什么了？\n"),
        )
        return 0 if first == second == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
