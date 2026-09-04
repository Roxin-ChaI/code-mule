from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from code_mule.cli.composition import ProductionCliComposition
from code_mule.conversation import (
    BossConversationService,
    BossIntent,
    BossSession,
    DeterministicBossIntentRouter,
    RoutedIntent,
)
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


class FixedChangeRouter:
    def route(self, message, session):
        return RoutedIntent(BossIntent.CHANGE, message)


class LocalBossChatE2E(unittest.TestCase):
    def test_natural_language_stop_matches_cli_cancellation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            state_file = root / "project-state.json"
            composition = ProductionCliComposition(
                state_file, environment={}, stdout=StringIO(), stderr=StringIO()
            )
            composition.init_project("stop-chat", "Stop Chat", workspace)
            before = JsonProjectStateStore(state_file).load()
            reply = BossConversationService(
                state_loader=JsonProjectStateStore(state_file).load,
                commands=composition,
                router=DeterministicBossIntentRouter(),
                session=BossSession(before.project.id),
            ).handle("这个项目不做了")
            after = JsonProjectStateStore(state_file).load()
            self.assertIs(reply.intent, BossIntent.STOP)
            self.assertIn("PROJECT CANCELLED", reply.lines)
            self.assertIn("Completed work was preserved.", reply.lines)
            self.assertIs(after.project.status, ProjectStatus.CANCELLED)

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


class ChangeResponseConsistencyE2E(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.state_file = root / "project-state.json"
        self.composition = ProductionCliComposition(
            self.state_file,
            environment={},
            stdout=StringIO(),
            stderr=StringIO(),
        )
        self.composition.init_project(
            "change-chat", "Change Chat", self.workspace
        )
        self.store = JsonProjectStateStore(self.state_file)

    def tearDown(self):
        self.temporary.cleanup()

    def conversation(self, router):
        state = self.store.load()
        return BossConversationService(
            state_loader=self.store.load,
            commands=self.composition,
            router=router,
            session=BossSession(state.project.id),
        )

    def test_running_change_reports_success_only_after_persisted_transition(self):
        state = self.store.load()
        self.store.save(
            replace(
                state,
                project=replace(state.project, status=ProjectStatus.RUNNING),
            )
        )

        reply = self.conversation(FixedChangeRouter()).handle(
            "再加一个导出 JSON 的功能"
        )

        final = self.store.load()
        rendered = "\n".join(reply.lines)
        self.assertEqual(final.project.status, ProjectStatus.CHANGE_REQUESTED)
        self.assertEqual(len(final.change_requests), 1)
        self.assertIn("Change recorded", rendered)
        self.assertIn("Apply impact analysis", rendered)
        self.assertEqual(reply.referenced_change_id, final.change_requests[0].id)

    def test_done_change_reports_only_failure_and_keeps_state_unchanged(self):
        state = self.store.load()
        self.store.save(
            replace(state, project=replace(state.project, status=ProjectStatus.DONE))
        )
        before = self.store.load()

        reply = self.conversation(FixedChangeRouter()).handle(
            "再加一个导出 JSON 的功能"
        )

        rendered = "\n".join(reply.lines)
        self.assertEqual(self.store.load(), before)
        self.assertIn("not valid in the current project state", rendered)
        self.assertIn("No project state was changed", rendered)
        self.assertNotIn("Change recorded", rendered)
        self.assertNotIn("impact analysis", rendered)
        self.assertIsNone(reply.referenced_change_id)

    def test_human_required_change_has_no_success_text(self):
        state = self.store.load()
        self.store.save(
            replace(
                state,
                project=replace(
                    state.project, status=ProjectStatus.HUMAN_REQUIRED
                ),
            )
        )
        before = self.store.load()

        reply = self.conversation(FixedChangeRouter()).handle("增加导出 JSON")

        rendered = "\n".join(reply.lines)
        self.assertEqual(self.store.load(), before)
        self.assertNotIn("Change recorded", rendered)
        self.assertNotIn("impact analysis", rendered)
        self.assertIsNone(reply.referenced_change_id)

    def test_ambiguous_change_language_is_unknown_and_read_only(self):
        state = self.store.load()
        self.store.save(
            replace(
                state,
                project=replace(state.project, status=ProjectStatus.RUNNING),
            )
        )
        before = self.store.load()

        reply = self.conversation(DeterministicBossIntentRouter()).handle(
            "也许可以考虑一个导出功能"
        )

        self.assertIs(reply.intent, BossIntent.UNKNOWN)
        self.assertEqual(self.store.load(), before)
        self.assertNotIn("Change recorded", "\n".join(reply.lines))


if __name__ == "__main__":
    unittest.main()
