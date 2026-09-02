from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli.composition import ProductionCliComposition
from code_mule.conversation import BossIntent, RoutedIntent
from code_mule.domain import Milestone, Plan, PlanStatus, ProjectStatus, Task, TaskStatus
from code_mule.state.store import JsonProjectStateStore


class ScriptedRouter:
    """A non-model router used to exercise real persisted Boss commands."""

    def __init__(self):
        self.calls = []

    def route(self, message, session):
        self.calls.append(message)
        routes = {
            "计划是什么？": RoutedIntent(BossIntent.QUERY_PLAN, "plan"),
            "现在做到哪了？": RoutedIntent(BossIntent.QUERY_PROGRESS, "progress"),
            "增加 multiply": RoutedIntent(BossIntent.CHANGE, "增加 multiply"),
            "先暂停": RoutedIntent(BossIntent.PAUSE, "pause"),
        }
        return routes[message]


class LocalBossChatE2E(unittest.TestCase):
    def test_chat_matches_real_cli_change_and_fail_closed_pause_semantics(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            state_file = root / "project-state.json"
            output = StringIO()
            composition = ProductionCliComposition(
                state_file,
                environment={},
                stdout=output,
                stderr=StringIO(),
            )
            composition.init_project("chat-project", "Chat Project", workspace)
            store = JsonProjectStateStore(state_file)
            state = store.load()
            now = datetime.now(UTC)
            plan = Plan(
                "PLAN-1", state.project.id, 1, PlanStatus.ACTIVE, (), ("M1",), now
            )
            milestone = Milestone("M1", plan.id, "Foundation", "active", ("T1",))
            task = Task(
                "T1",
                milestone.id,
                "Implement foundation",
                "Implement foundation",
                TaskStatus.PENDING,
                (),
                ("tests pass",),
                0,
                now,
                now,
            )
            store.save(
                replace(
                    state,
                    project=replace(
                        state.project,
                        status=ProjectStatus.RUNNING,
                        active_plan_id=plan.id,
                    ),
                    plans=(plan,),
                    milestones=(milestone,),
                    tasks=(task,),
                )
            )
            router = ScriptedRouter()
            composition._boss_intent_router = lambda: router

            result = composition.chat(
                StringIO(
                    "计划是什么？\n"
                    "现在做到哪了？\n"
                    "增加 multiply\n"
                    "先暂停\n"
                )
            )

            final = store.load()
            rendered = output.getvalue()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                router.calls,
                ["计划是什么？", "现在做到哪了？", "增加 multiply", "先暂停"],
            )
            self.assertIn("Plan v1", rendered)
            self.assertIn("1 tasks; 0 completed", rendered)
            self.assertIn("CHANGE REQUESTED", rendered)
            self.assertIn("not valid in the current project state", rendered)
            self.assertEqual(final.project.status, ProjectStatus.CHANGE_REQUESTED)
            self.assertEqual(len(final.change_requests), 1)
            self.assertEqual(final.change_requests[0].description, "增加 multiply")
            self.assertNotIn("project_status: change_requested", rendered)


if __name__ == "__main__":
    unittest.main()
