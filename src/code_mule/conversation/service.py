"""Conversation orchestration over existing typed Boss command capabilities."""

from __future__ import annotations

from typing import Callable, Protocol

from code_mule.cli.contracts import CliCommandResult, CliError, CliExitCode
from code_mule.domain.enums import (
    HumanActionStatus,
    HumanResolutionStrategy,
    ProjectStatus,
    TaskStatus,
)
from code_mule.presentation import (
    decision_label,
    project_view,
    render_human_action,
    render_project,
)
from code_mule.state.models import ProjectState

from .contracts import (
    BossIntent,
    BossIntentRouter,
    BossSession,
    ConversationReply,
    RoutedIntent,
)


class BossCommandGateway(Protocol):
    def ask(self, question: str, verbose: bool = False) -> CliCommandResult: ...
    def change(self, request: str, verbose: bool = False) -> CliCommandResult: ...
    def pause(self, verbose: bool = False) -> CliCommandResult: ...
    def resume(self, verbose: bool = False) -> CliCommandResult: ...
    def inspect(self, verbose: bool = False) -> CliCommandResult: ...
    def approve(self, action_id: str, verbose: bool = False) -> CliCommandResult: ...
    def reject(self, action_id: str, verbose: bool = False) -> CliCommandResult: ...
    def resolve(
        self,
        action_id: str,
        strategy: HumanResolutionStrategy,
        verbose: bool = False,
    ) -> CliCommandResult: ...


class BossConversationService:
    """Route language, read deterministic facts, and delegate typed commands."""

    def __init__(
        self,
        *,
        state_loader: Callable[[], ProjectState],
        commands: BossCommandGateway,
        router: BossIntentRouter,
        session: BossSession,
        verbose: bool = False,
    ) -> None:
        self._state_loader = state_loader
        self._commands = commands
        self._router = router
        self._session = session
        self._verbose = verbose

    def handle(self, message: str) -> ConversationReply:
        try:
            routed = self._router.route(message, self._session)
        except Exception:
            routed = RoutedIntent(
                BossIntent.UNKNOWN,
                "clarify",
                0.0,
                "router unavailable",
            )
        state = self._state_loader()
        reply = self._dispatch(routed, state)
        if self._verbose:
            reply = self._with_verbose(reply, routed, self._state_loader())
        self._session.record(message, reply)
        return reply

    def _dispatch(
        self, routed: RoutedIntent, state: ProjectState
    ) -> ConversationReply:
        intent = routed.intent
        if intent is BossIntent.QUERY_STATUS:
            return ConversationReply(intent, render_project(state))
        if intent is BossIntent.QUERY_PLAN:
            return self._plan_reply(state)
        if intent is BossIntent.QUERY_PROGRESS:
            return self._progress_reply(state)
        if intent is BossIntent.QUERY_CURRENT_WORK:
            return self._current_work_reply(state)
        if intent is BossIntent.QUERY_BLOCKERS:
            return self._blockers_reply(state)
        if intent is BossIntent.QUERY_GENERAL:
            return self._command_reply(intent, lambda: self._commands.ask(routed.normalized_request))
        if intent is BossIntent.CHANGE:
            return self._change_reply(state, routed.normalized_request)
        if intent is BossIntent.PAUSE:
            return self._command_reply(intent, self._commands.pause)
        if intent is BossIntent.RESUME:
            return self._command_reply(intent, self._commands.resume)
        if intent is BossIntent.INSPECT:
            return self._inspect_reply(state)
        if intent is BossIntent.APPROVE:
            return self._human_decision(
                state, routed.normalized_request, approve=True
            )
        if intent is BossIntent.REJECT:
            return self._human_decision(
                state, routed.normalized_request, approve=False
            )
        if intent is BossIntent.RESOLVE:
            return self._resolve_reply(state, routed.normalized_request)
        if intent is BossIntent.HELP:
            return ConversationReply(
                intent,
                (
                    "You can ask about status, plan, progress, current work, or blockers.",
                    "You can also request a change, pause, resume, inspect, approve, reject, or retry.",
                    "Ambiguous requests are clarified before any state change.",
                ),
            )
        return ConversationReply(
            BossIntent.UNKNOWN,
            (
                "I could not determine a safe intent from that request.",
                "Please clarify whether you want a query, change, pause, resume, or Human Action decision.",
                "No project state was changed.",
            ),
        )

    @staticmethod
    def _command_reply(
        intent: BossIntent, operation: Callable[[], CliCommandResult]
    ) -> ConversationReply:
        try:
            result = operation()
        except CliError as error:
            return ConversationReply(
                intent,
                (
                    f"That {intent.value.replace('_', ' ')} request is not valid in the current project state.",
                    error.public_message,
                    "No unsafe operation was performed.",
                ),
            )
        return ConversationReply(intent, result.output or ("Done.",))

    def _change_reply(
        self, state: ProjectState, request: str
    ) -> ConversationReply:
        existing_ids = {change.id for change in state.change_requests}
        try:
            result = self._commands.change(request)
        except CliError as error:
            return ConversationReply(
                BossIntent.CHANGE,
                (
                    "That change request is not valid in the current project state.",
                    error.public_message,
                    "No project state was changed.",
                ),
            )
        if result.exit_code is not CliExitCode.SUCCESS:
            return ConversationReply(
                BossIntent.CHANGE,
                (
                    "The change command did not report a successful typed outcome.",
                    "No change success was accepted.",
                ),
            )
        latest = self._state_loader()
        added = tuple(
            change
            for change in latest.change_requests
            if change.id not in existing_ids
        )
        if (
            latest.project.status is not ProjectStatus.CHANGE_REQUESTED
            or len(added) != 1
        ):
            return ConversationReply(
                BossIntent.CHANGE,
                (
                    "The change command did not produce a valid CHANGE_REQUESTED outcome.",
                    "No change success was accepted.",
                ),
            )
        return ConversationReply(
            BossIntent.CHANGE,
            result.output
            + ("", "Change recorded. Apply impact analysis explicitly when ready."),
            referenced_change_id=added[0].id,
        )

    def _inspect_reply(self, state: ProjectState) -> ConversationReply:
        pending = self._pending_actions(state)
        if not pending:
            return ConversationReply(
                BossIntent.INSPECT,
                ("No pending Human Action requires a decision.",),
            )
        if len(pending) > 1:
            return self._multiple_actions_reply(BossIntent.INSPECT, pending)
        action = pending[0]
        return ConversationReply(
            BossIntent.INSPECT,
            render_human_action(action),
            referenced_action_id=action.id,
            referenced_task_id=action.task_id,
        )

    def _human_decision(
        self,
        state: ProjectState,
        normalized_request: str,
        *,
        approve: bool,
    ) -> ConversationReply:
        intent = BossIntent.APPROVE if approve else BossIntent.REJECT
        pending = self._pending_actions(state)
        action = self._selected_action(pending, normalized_request)
        if action is None:
            return (
                self._multiple_actions_reply(intent, pending)
                if pending
                else ConversationReply(intent, ("No pending Human Action is available.",))
            )
        result = self._command_reply(
            intent,
            (lambda: self._commands.approve(action.id))
            if approve
            else (lambda: self._commands.reject(action.id)),
        )
        return ConversationReply(
            intent,
            result.lines,
            referenced_action_id=action.id,
            referenced_task_id=action.task_id,
        )

    def _resolve_reply(
        self, state: ProjectState, normalized_request: str
    ) -> ConversationReply:
        pending = self._pending_actions(state)
        action = self._selected_action(pending, normalized_request)
        if action is None:
            return (
                self._multiple_actions_reply(BossIntent.RESOLVE, pending)
                if pending
                else ConversationReply(
                    BossIntent.RESOLVE, ("No pending Human Action is available.",)
                )
            )
        if (
            normalized_request != HumanResolutionStrategy.RETRY_TASK.value
            and action.id != normalized_request
        ):
            return ConversationReply(
                BossIntent.RESOLVE,
                ("Please specify a supported resolution strategy.",),
            )
        result = self._command_reply(
            BossIntent.RESOLVE,
            lambda: self._commands.resolve(
                action.id, HumanResolutionStrategy.RETRY_TASK
            ),
        )
        return ConversationReply(
            BossIntent.RESOLVE,
            result.lines,
            referenced_action_id=action.id,
            referenced_task_id=action.task_id,
        )

    @staticmethod
    def _pending_actions(state: ProjectState):
        return tuple(
            action
            for action in state.human_actions
            if action.status is HumanActionStatus.PENDING
        )

    @staticmethod
    def _selected_action(actions, normalized_request: str):
        if len(actions) == 1:
            return actions[0]
        matches = tuple(
            action for action in actions if action.id == normalized_request
        )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _multiple_actions_reply(intent: BossIntent, actions) -> ConversationReply:
        return ConversationReply(
            intent,
            (
                "Multiple Human Actions are pending; choose an exact action ID:",
                *(f"- {action.id}: {action.summary}" for action in actions),
                "No action was taken.",
            ),
        )

    @staticmethod
    def _active_graph(state: ProjectState):
        plan = next(
            (item for item in state.plans if item.id == state.project.active_plan_id),
            None,
        )
        if plan is None:
            return None, (), ()
        milestones = tuple(
            milestone
            for milestone_id in plan.milestone_ids
            for milestone in state.milestones
            if milestone.id == milestone_id
        )
        task_ids = tuple(task_id for milestone in milestones for task_id in milestone.task_ids)
        tasks = tuple(
            task for task_id in task_ids for task in state.tasks if task.id == task_id
        )
        return plan, milestones, tasks

    def _plan_reply(self, state: ProjectState) -> ConversationReply:
        plan, milestones, tasks = self._active_graph(state)
        if plan is None:
            return ConversationReply(BossIntent.QUERY_PLAN, ("No active Plan exists.",))
        task_by_id = {task.id: task for task in tasks}
        lines: tuple[str, ...] = (
            f"Plan v{plan.version} · {len(milestones)} milestones · {len(tasks)} tasks",
            "",
        )
        for index, milestone in enumerate(milestones, 1):
            lines += (f"{index}. {milestone.title}",)
            for task_id in milestone.task_ids:
                task = task_by_id[task_id]
                symbol = self._task_symbol(task, state.project.current_task_id)
                dependency = (
                    "" if not task.dependencies else f" · depends on {', '.join(task.dependencies)}"
                )
                lines += (f"   {symbol} {task.id} · {task.title}{dependency}",)
            lines += ("",)
        current = next(
            (task for task in tasks if task.id == state.project.current_task_id), None
        )
        lines += (
            "Current position: "
            + ("No task is executing." if current is None else f"{current.id} · {current.title}"),
        )
        return ConversationReply(
            BossIntent.QUERY_PLAN,
            lines,
            referenced_task_id=None if current is None else current.id,
        )

    def _progress_reply(self, state: ProjectState) -> ConversationReply:
        view = project_view(state)
        plan = "No active Plan" if view.plan_version is None else f"Plan v{view.plan_version}"
        current = (
            "No task is currently executing."
            if view.current_task is None
            else f"Current work: {view.current_task}."
        )
        return ConversationReply(
            BossIntent.QUERY_PROGRESS,
            (
                f"{plan} has {view.total_tasks} tasks; {view.completed_tasks} completed.",
                current,
                f"Project status: {view.status}.",
            ),
            referenced_task_id=state.project.current_task_id,
        )

    def _current_work_reply(self, state: ProjectState) -> ConversationReply:
        current = next(
            (task for task in state.tasks if task.id == state.project.current_task_id),
            None,
        )
        if current is None:
            return ConversationReply(
                BossIntent.QUERY_CURRENT_WORK,
                ("No task is currently executing.",),
            )
        report = next(
            (item for item in reversed(state.execution_reports) if item.task_id == current.id),
            None,
        )
        decision = next(
            (item for item in reversed(state.decisions) if item.task_id == current.id),
            None,
        )
        lines = (
            f"Current work: {current.id} · {current.title}",
            f"Attempt: {current.execution_attempts + 1}",
        )
        if report is not None:
            lines += (f"Latest Worker report: {report.summary}",)
        if decision is not None:
            lines += (f"Latest review: {decision_label(decision.type)}",)
        return ConversationReply(
            BossIntent.QUERY_CURRENT_WORK,
            lines,
            referenced_task_id=current.id,
        )

    def _blockers_reply(self, state: ProjectState) -> ConversationReply:
        _, _, tasks = self._active_graph(state)
        blocked = tuple(task for task in tasks if task.status is TaskStatus.BLOCKED)
        pending_actions = self._pending_actions(state)
        human_required = state.project.status is ProjectStatus.HUMAN_REQUIRED
        if not blocked and not pending_actions and not human_required:
            return ConversationReply(
                BossIntent.QUERY_BLOCKERS,
                ("目前没有需要你处理的问题。",),
            )
        lines: tuple[str, ...] = ("Current blockers:",)
        lines += tuple(f"- {task.id} · {task.title}" for task in blocked)
        lines += tuple(
            f"- Boss action required: {action.summary}" for action in pending_actions
        )
        if human_required and not pending_actions:
            lines += ("- The project is safely stopped and requires human review.",)
        if pending_actions:
            lines += ("Say '发生什么了？' or use inspect to review the action.",)
        return ConversationReply(BossIntent.QUERY_BLOCKERS, lines)

    @staticmethod
    def _task_symbol(task, current_task_id: str | None) -> str:
        if task.id == current_task_id or task.status is TaskStatus.IN_PROGRESS:
            return "→"
        if task.status is TaskStatus.COMPLETED:
            return "✓"
        if task.status is TaskStatus.BLOCKED:
            return "!"
        if task.status is TaskStatus.CANCELLED:
            return "×"
        return "○"

    @staticmethod
    def _with_verbose(
        reply: ConversationReply, routed: RoutedIntent, state: ProjectState
    ) -> ConversationReply:
        lines = reply.lines + (
            "",
            "ROUTING",
            f"intent: {routed.intent.value}",
            f"confidence: {routed.confidence:.2f}",
            f"project_status: {state.project.status.value}",
            f"active_plan_id: {state.project.active_plan_id or '-'}",
            f"referenced_task_id: {reply.referenced_task_id or '-'}",
            f"referenced_action_id: {reply.referenced_action_id or '-'}",
            f"referenced_change_id: {reply.referenced_change_id or '-'}",
        )
        return ConversationReply(
            reply.intent,
            lines,
            reply.referenced_task_id,
            reply.referenced_action_id,
            reply.referenced_change_id,
        )


__all__ = ["BossCommandGateway", "BossConversationService"]
