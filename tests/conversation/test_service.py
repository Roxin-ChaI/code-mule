from dataclasses import replace
from io import StringIO
import unittest

from code_mule.cli import CliCommandResult, CliExitCode, InvalidCliProjectState
from code_mule.conversation import (
    BossConversationService,
    BossIntent,
    BossSession,
    ConversationReply,
    DeterministicBossIntentRouter,
    RoutedIntent,
    run_chat_loop,
)
from code_mule.domain import (
    HumanAction,
    HumanActionCategory,
    HumanActionStatus,
    ProjectStatus,
    TaskStatus,
)
from state import UPDATED, make_project_state


class FixedRouter:
    def __init__(self, intent, normalized="request"):
        self.routed = RoutedIntent(intent, normalized)
    def route(self, message, session): return self.routed


class ExplodingRouter:
    def route(self, message, session): raise RuntimeError("sk-secret-router")


class FakeGateway:
    def __init__(self): self.calls = []
    def _call(self, name, *values):
        self.calls.append((name, values))
        return CliCommandResult(CliExitCode.SUCCESS, (f"{name} complete",))
    def ask(self, *values): return self._call("ask", *values)
    def change(self, *values): return self._call("change", *values)
    def pause(self, *values): return self._call("pause", *values)
    def resume(self, *values): return self._call("resume", *values)
    def inspect(self, *values): return self._call("inspect", *values)
    def approve(self, *values): return self._call("approve", *values)
    def reject(self, *values): return self._call("reject", *values)
    def resolve(self, *values): return self._call("resolve", *values)


class FailingGateway(FakeGateway):
    def pause(self, *values):
        raise InvalidCliProjectState("pause is invalid for current state")


def service(state, intent, *, gateway=None, verbose=False, normalized="request"):
    gateway = gateway or FakeGateway()
    instance = BossConversationService(
        state_loader=lambda: state,
        commands=gateway,
        router=FixedRouter(intent, normalized),
        session=BossSession(state.project.id),
        verbose=verbose,
    )
    return instance, gateway


class BossConversationServiceTests(unittest.TestCase):
    def test_status_plan_progress_current_and_blockers_use_state_facts(self):
        state = make_project_state()
        blocked = replace(state.tasks[0], status=TaskStatus.BLOCKED)
        state = replace(state, tasks=(blocked,))
        cases = {
            BossIntent.QUERY_STATUS: ("Status      Running",),
            BossIntent.QUERY_PLAN: ("Plan v1", "Persistence", "task-1"),
            BossIntent.QUERY_PROGRESS: ("1 tasks; 0 completed", "Current work"),
            BossIntent.QUERY_CURRENT_WORK: ("task-1 · Serialize state", "Attempt: 3"),
            BossIntent.QUERY_BLOCKERS: ("Current blockers", "task-1"),
        }
        for intent, fragments in cases.items():
            with self.subTest(intent=intent):
                conversation, gateway = service(state, intent)
                output = "\n".join(conversation.handle("query").lines)
                for fragment in fragments:
                    self.assertIn(fragment, output)
                self.assertEqual(gateway.calls, [])
                self.assertNotIn("project_status: running", output)

    def test_change_pause_resume_delegate_only_existing_commands(self):
        state = make_project_state()
        cases = (
            (BossIntent.CHANGE, "增加 multiply", "change"),
            (BossIntent.PAUSE, "pause", "pause"),
            (BossIntent.RESUME, "resume", "resume"),
        )
        for intent, normalized, expected in cases:
            with self.subTest(intent=intent):
                conversation, gateway = service(
                    state, intent, normalized=normalized
                )
                conversation.handle(normalized)
                self.assertEqual(gateway.calls[0][0], expected)
        self.assertNotIn("apply", gateway.calls[0][0])

    def test_unknown_and_router_failure_clarify_without_side_effect_or_secret(self):
        state = make_project_state()
        gateway = FakeGateway()
        session = BossSession(state.project.id)
        for router in (FixedRouter(BossIntent.UNKNOWN), ExplodingRouter()):
            conversation = BossConversationService(
                state_loader=lambda: state,
                commands=gateway,
                router=router,
                session=session,
            )
            output = "\n".join(conversation.handle("ambiguous").lines)
            self.assertIn("clarify", output)
            self.assertNotIn("sk-secret", output)
        self.assertEqual(gateway.calls, [])

    def test_unique_human_action_routes_approve_and_reject(self):
        state = make_project_state()
        action = HumanAction(
            "action-1", state.project.id, "task-1",
            HumanActionCategory.WORKER_APPROVAL,
            "Approval", "Approve exact operation", "External effect",
            HumanActionStatus.PENDING, UPDATED,
        )
        state = replace(
            state,
            project=replace(state.project, status=ProjectStatus.HUMAN_REQUIRED),
            human_actions=(action,),
        )
        for intent, expected in (
            (BossIntent.APPROVE, "approve"),
            (BossIntent.REJECT, "reject"),
        ):
            with self.subTest(intent=intent):
                conversation, gateway = service(state, intent)
                reply = conversation.handle(intent.value)
                self.assertEqual(gateway.calls, [(expected, ("action-1",))])
                self.assertEqual(reply.referenced_action_id, "action-1")

    def test_multiple_human_actions_require_exact_id_clarification(self):
        state = make_project_state()
        first = HumanAction(
            "action-1", state.project.id, None,
            HumanActionCategory.WORKER_APPROVAL,
            "First", "Approve first", "Risk",
            HumanActionStatus.PENDING, UPDATED,
        )
        second = replace(first, id="action-2", summary="Second")
        state = replace(
            state,
            project=replace(state.project, status=ProjectStatus.HUMAN_REQUIRED),
            human_actions=(first, second),
        )
        conversation, gateway = service(state, BossIntent.APPROVE)
        output = "\n".join(conversation.handle("批准").lines)
        self.assertIn("choose an exact action ID", output)
        self.assertIn("action-1", output)
        self.assertIn("action-2", output)
        self.assertEqual(gateway.calls, [])

        selected, selected_gateway = service(
            state,
            BossIntent.APPROVE,
            normalized="action-2",
        )
        selected.handle("approve action-2")
        self.assertEqual(selected_gateway.calls, [("approve", ("action-2",))])

    def test_verbose_adds_route_and_raw_state_only_when_requested(self):
        state = make_project_state()
        normal, _ = service(state, BossIntent.QUERY_STATUS)
        verbose, _ = service(state, BossIntent.QUERY_STATUS, verbose=True)
        self.assertNotIn("intent:", "\n".join(normal.handle("status").lines))
        output = "\n".join(verbose.handle("status").lines)
        self.assertIn("intent: query_status", output)
        self.assertIn("project_status: running", output)
        self.assertIn("active_plan_id: plan-1", output)

    def test_command_state_error_is_boss_readable(self):
        state = make_project_state()
        conversation, _ = service(
            state, BossIntent.PAUSE, gateway=FailingGateway()
        )
        output = "\n".join(conversation.handle("暂停").lines)
        self.assertIn("not valid in the current project state", output)
        self.assertIn("No unsafe operation", output)


class ChatLoopTests(unittest.TestCase):
    def test_multiple_messages_share_session_and_eof_exits_cleanly(self):
        state = make_project_state()
        session = BossSession(state.project.id, max_history=2)
        conversation = BossConversationService(
            state_loader=lambda: state,
            commands=FakeGateway(),
            router=DeterministicBossIntentRouter(),
            session=session,
        )
        output = StringIO()
        run_chat_loop(
            conversation,
            input_stream=StringIO("状态\n计划是什么？\n"),
            output_stream=output,
        )
        rendered = output.getvalue()
        self.assertEqual(rendered.count("You > "), 3)
        self.assertIn("Code Mule > PROJECT", rendered)
        self.assertIn("Code Mule > Plan v1", rendered)
        self.assertIn("Goodbye", rendered)
        self.assertEqual(session.recent_boss_messages, ("状态", "计划是什么？"))


if __name__ == "__main__":
    unittest.main()
