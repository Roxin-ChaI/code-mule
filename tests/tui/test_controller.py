"""Controller behaviour: dispatch, HumanAction handling, refresh, and safety."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_mule.cli.contracts import CliExitCode
from code_mule.domain.enums import HumanActionStatus, ProjectStatus
from code_mule.domain.models import ProjectEvent
from code_mule.progress.contracts import ProgressEvent, ProgressEventType
from code_mule.state.serialization import serialize_project_state
from code_mule.state.store import JsonProjectStateStore
from code_mule.tui.activity import ActivityKind
from code_mule.tui.app import availability, fallback_lines, run_terminal
from code_mule.tui.controller import TerminalController
from code_mule.tui.demo import DemoCommands, demo_commands, demo_state
from code_mule.tui.layout import compute_layout
from code_mule.tui.snapshot import build_snapshot, boss_lines

from state import make_project_state


NOW = datetime(2026, 9, 18, tzinfo=UTC)


def controller(commands=None, state=None, **kwargs) -> TerminalController:
    payload = state if state is not None else demo_state()
    built = TerminalController(
        commands or demo_commands(),
        lambda: payload,
        input_stream=io.StringIO(""),
    )
    return built


class InputAndDispatchTests(unittest.TestCase):
    def test_typing_and_backspace_update_only_the_view_buffer(self):
        control = controller()
        control.insert("sta")
        control.insert("tus")
        self.assertEqual(control.buffered_input, "status")
        control.backspace()
        self.assertEqual(control.buffered_input, "statu")
        self.assertEqual(control.project_state().project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_dispatch_uses_the_shared_cli_grammar(self):
        commands = DemoCommands()
        control = controller(commands)
        result = control.dispatch("status")
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(commands.calls, ["status"])
        self.assertEqual(control.buffered_input, "")
        self.assertIn("boss> status", [entry.text for entry in control.activity.entries])

    def test_invalid_command_is_reported_without_touching_state(self):
        control = controller()
        result = control.dispatch("not-a-command")
        self.assertEqual(result.exit_code, CliExitCode.INVALID_USAGE)
        self.assertTrue(any("Invalid command" in entry.text for entry in control.activity.entries))

    def test_action_dispatch_reaches_the_command_layer_once(self):
        for line, expected in (
            ("resolve action-demo --strategy acknowledge", "resolve"),
            ("approve action-demo", "approve"),
            ("reject action-demo", "reject"),
            ("answer action-demo yes", "answer"),
            ("inspect --verbose", "inspect"),
        ):
            with self.subTest(line=line):
                commands = DemoCommands()
                control = controller(commands)
                control.dispatch(line)
                self.assertEqual(commands.calls, [expected])

    def test_resolve_receives_the_typed_strategy(self):
        seen: list[str] = []

        class Recorder(DemoCommands):
            def resolve(self, action_id, strategy, verbose=False):
                seen.append(f"{action_id}:{strategy.value}")
                return super().resolve(action_id, strategy, verbose)

        control = controller(Recorder())
        control.dispatch("resolve action-demo --strategy fail_project")
        self.assertEqual(seen, ["action-demo:fail_project"])

    def test_failing_command_is_surfaced_not_hidden(self):
        class Failing(DemoCommands):
            def status(self, verbose: bool = False):
                from code_mule.cli.contracts import CliExecutionFailure

                raise CliExecutionFailure("provider unavailable")

        control = controller(Failing())
        result = control.dispatch("status")
        self.assertEqual(result.exit_code, CliExitCode.PROVIDER_OR_WORKER_FAILURE)
        self.assertIn("provider unavailable", " ".join(result.output))

    def test_ui_never_writes_project_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project-state.json"
            store = JsonProjectStateStore(path)
            store.save(make_project_state())
            before = path.read_bytes()
            control = TerminalController(
                demo_commands(), lambda: store.load(), input_stream=io.StringIO("")
            )
            control.dispatch("status")
            control.dispatch("launch")
            control.dispatch("inspect --verbose")
            self.assertEqual(path.read_bytes(), before)

    def test_done_project_still_accepts_boss_commands(self):
        state = replace(
            make_project_state(), project=replace(
                make_project_state().project, status=ProjectStatus.DONE
            )
        )
        commands = DemoCommands()
        control = controller(commands, state=state)
        for line in ("status", "launch", "change add a health endpoint"):
            result = control.dispatch(line)
            self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertEqual(commands.calls, ["status", "launch", "change"])
        self.assertFalse(control.should_quit)

    def test_explicit_exit_is_the_only_command_that_closes_the_session(self):
        state = replace(
            make_project_state(),
            project=replace(make_project_state().project, status=ProjectStatus.DONE),
            human_actions=(),
        )
        control = controller(state=state)
        control.dispatch("status")
        self.assertFalse(control.should_quit)
        result = control.dispatch("exit")
        self.assertEqual(result.exit_code, CliExitCode.SUCCESS)
        self.assertTrue(control.should_quit)
        self.assertEqual(control.project_state().project.status, ProjectStatus.DONE)

    def test_only_one_command_runs_at_a_time(self):
        control = controller()
        control.state.busy = True
        control.insert("status")
        control.submit_async()
        self.assertIn(
            "A command is already running.",
            [entry.text for entry in control.activity.entries],
        )


class HumanActionRenderingTests(unittest.TestCase):
    def test_boss_pane_renders_the_pending_action_and_its_exact_actions(self):
        state = demo_state()
        lines = boss_lines(state, pending=True, buffer="")
        text = "\n".join(lines)
        self.assertIn("HUMAN ACTION · Worker input", text)
        self.assertIn("Request: Confirm the manifest entry point before delivery", text)
        # The main UI stays compact: one short Actions line, not a full command.
        self.assertIn("Actions: fail_project | acknowledge | answer", text)
        self.assertNotIn("--strategy", text)
        self.assertNotIn("resolve action-demo", text)
        self.assertEqual(lines[-1], "boss> ")

    def test_boss_pane_is_just_the_prompt_without_a_pending_action(self):
        state = replace(make_project_state(), human_actions=())
        lines = boss_lines(state, pending=True, buffer="abc")
        self.assertEqual(lines, ("boss> abc",))

    def test_no_action_is_ever_approved_automatically(self):
        commands = DemoCommands()
        control = controller(commands)
        control.insert("status")
        control.submit()
        self.assertEqual(commands.calls, ["status"])
        state = control.project_state()
        self.assertEqual(
            [action.status for action in state.human_actions], [HumanActionStatus.PENDING]
        )

    def test_snapshot_exposes_all_three_panes(self):
        snapshot = build_snapshot(demo_state(), ("one", "two"), buffer="q", now=NOW)
        self.assertTrue(snapshot.status[0].startswith("CODE MULE"))
        self.assertEqual(snapshot.activity, ("one", "two"))
        self.assertTrue(snapshot.pane("boss")[-1].startswith("boss>"))
        with self.assertRaises(ValueError):
            snapshot.pane("nope")


class RefreshAndEventTests(unittest.TestCase):
    def test_newly_persisted_events_are_picked_up_without_duplicates(self):
        state = make_project_state()
        events = tuple(
            ProjectEvent(f"e{index}", state.project.id, "task.started", "task-1", NOW, {})
            for index in range(2)
        )
        control = controller(state=replace(state, events=events))
        self.assertTrue(control.refresh_activity_from_events())
        self.assertFalse(control.refresh_activity_from_events())
        self.assertEqual(len(control.activity), 2)

    def test_state_changes_between_reads_are_reflected(self):
        base = make_project_state()
        current = {"state": base}
        control = TerminalController(
            demo_commands(), lambda: current["state"], input_stream=io.StringIO("")
        )
        self.assertEqual(control.project_state().project.status, ProjectStatus.RUNNING)
        current["state"] = replace(
            base, project=replace(base.project, status=ProjectStatus.DONE)
        )
        self.assertEqual(control.project_state().project.status, ProjectStatus.DONE)

    def test_progress_events_feed_activity_without_payload(self):
        control = controller()
        event = ProgressEvent(
            ProgressEventType.WORKER_ACTIVITY, NOW, "project-1", "task-1", 1,
            "Running command", {"activity": "command_execution.started"},
        )
        self.assertTrue(control.record_progress_event(event))
        entry = control.activity.entries[-1]
        self.assertEqual(entry.kind, ActivityKind.RESPONSE)
        self.assertIn("Running command", entry.text)
        self.assertNotIn("command_execution.started", entry.text)

    def test_runtime_and_recovery_states_render_in_the_status_pane(self):
        from code_mule.recovery import ExecutionStopReason, SafePoint, SafePointKind

        state = replace(
            make_project_state(),
            latest_safe_point=SafePoint(SafePointKind.UNCERTAIN, NOW),
        )
        layout = compute_layout(24, 80, status_height=8, boss_height=4)
        snapshot = build_snapshot(state, (), now=NOW)
        text = "\n".join(
            snapshot.status
        )
        self.assertIn("Uncertain", text)
        self.assertGreaterEqual(layout.activity_height, 1)
        _ = ExecutionStopReason  # imported for the recovery vocabulary only


class StreamAndFallbackTests(unittest.TestCase):
    def test_done_tui_survives_unsupported_cursor_hiding_until_ctrl_c(self):
        import curses
        from code_mule.tui.app import _run_curses

        state = replace(
            make_project_state(),
            project=replace(make_project_state().project, status=ProjectStatus.DONE),
            human_actions=(),
        )
        control = controller(state=state)

        class Screen:
            def __init__(self):
                self.keys = iter((None, "\x03"))
                self.draws = 0

            def nodelay(self, _value): pass
            def timeout(self, _value): pass
            def keypad(self, _value): pass
            def getmaxyx(self): return (24, 80)
            def erase(self): self.draws += 1
            def addstr(self, *_values): pass
            def refresh(self): pass
            def get_wch(self): return next(self.keys)

        screen = Screen()
        with patch("curses.curs_set", side_effect=curses.error("unsupported")), patch(
            "curses.wrapper", side_effect=lambda callback: callback(screen)
        ):
            code = _run_curses(control, stdout=io.StringIO())

        self.assertEqual(code, 0)
        self.assertGreaterEqual(screen.draws, 2)
        self.assertTrue(control.should_quit)
        self.assertEqual(control.project_state().project.status, ProjectStatus.DONE)

    def test_draw_repaints_every_line_and_survives_resize(self):
        from code_mule.tui.app import _draw

        class FakeScreen:
            def __init__(self, rows: int, cols: int) -> None:
                self.rows, self.cols = rows, cols
                self.erased = 0
                self.cells: dict[tuple[int, int], str] = {}

            def getmaxyx(self):
                return self.rows, self.cols

            def erase(self) -> None:
                self.erased += 1
                self.cells.clear()

            def addstr(self, row, col, text) -> None:
                for offset, character in enumerate(text):
                    self.cells[(row, col + offset)] = character

            def refresh(self) -> None:  # pragma: no cover - no-op
                return None

        control = controller()
        for row_count, col_count in ((24, 80), (40, 120), (18, 60)):
            screen = FakeScreen(row_count, col_count)
            _draw(screen, control, rows=row_count, cols=col_count)
            self.assertEqual(screen.erased, 1, "every frame must clear first")
            painted_rows = {row for row, _col in screen.cells}
            self.assertEqual(painted_rows, set(range(row_count)))
            self.assertLessEqual(max(col for _row, col in screen.cells), col_count - 1)
            self.assertFalse(control.state.dirty)

    def test_tty_is_required_for_the_persistent_ui(self):
        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        self.assertTrue(availability(Tty(), Tty()).interactive)
        self.assertFalse(availability(io.StringIO(), io.StringIO()).interactive)

    def test_non_tty_falls_back_to_stable_text(self):
        control = controller()
        out = io.StringIO()
        code = run_terminal(control, stdin=io.StringIO(), stdout=out)
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("CODE MULE"))
        self.assertTrue(any(line.startswith("persistent UI unavailable") for line in lines))
        self.assertTrue(any(line.startswith("boss>") for line in lines))

    def test_ctrl_c_requests_quit_without_mutating_state(self):
        from code_mule.tui.app import _handle_key

        state = demo_state()
        control = controller(state=state)
        self.assertTrue(_handle_key(control, "\x03", rows=24, cols=80))
        self.assertTrue(control.should_quit)
        self.assertIn("not modified", control.state.status_message)
        self.assertEqual(control.project_state().project.status, ProjectStatus.HUMAN_REQUIRED)

    def test_key_handling_covers_scroll_resize_and_enter(self):
        import curses

        from code_mule.tui.app import _handle_key

        control = controller()
        for index in range(20):
            control.activity.append(NOW, ActivityKind.INFO, f"line {index}")
        self.assertTrue(_handle_key(control, curses.KEY_UP, rows=24, cols=80))
        self.assertEqual(control.activity.offset, 1)
        self.assertTrue(_handle_key(control, curses.KEY_END, rows=24, cols=80))
        self.assertTrue(control.activity.following)
        self.assertTrue(_handle_key(control, curses.KEY_RESIZE, rows=40, cols=120))
        control.insert("status")
        self.assertTrue(_handle_key(control, curses.KEY_ENTER, rows=24, cols=80))
        control.join(timeout=5)
        self.assertEqual(control.buffered_input, "")

    def test_auto_follow_after_scrolling_shows_the_newest_activity(self):
        import curses

        from code_mule.tui.app import _handle_key

        control = controller()
        old = make_project_state()
        control = TerminalController(
            demo_commands(), lambda: old, input_stream=io.StringIO("")
        )
        for index in range(30):
            control.activity.append(NOW, ActivityKind.INFO, f"line {index}")
        _handle_key(control, curses.KEY_PPAGE, rows=24, cols=80)
        self.assertFalse(control.activity.following)
        _handle_key(control, curses.KEY_END, rows=24, cols=80)
        self.assertTrue(control.activity.following)
        layout = compute_layout(24, 80, status_height=8, boss_height=4)
        snapshot = build_snapshot(
            old,
            tuple(entry.text for entry in control.activity.visible(layout.activity_height)),
            now=NOW,
        )
        lines = [line for line in snapshot.activity]
        self.assertEqual(lines[-1], "line 29")

    def test_boss_actions_stay_single_line_for_wide_and_narrow_terminals(self):
        from code_mule.presentation.terminal import display_width

        state = demo_state()
        lines = boss_lines(state, pending=True, buffer="")
        actions = next(line for line in lines if line.startswith("Actions:"))
        self.assertLess(display_width(actions), 80)
        self.assertNotIn("--strategy", actions)
        self.assertNotIn("resolve ", actions)

    def test_fallback_frame_is_bounded_for_small_terminals(self):
        control = controller()
        for rows, cols in ((24, 80), (10, 40)):
            with self.subTest(rows=rows, cols=cols):
                lines = fallback_lines(control, rows=rows, cols=cols)
                self.assertEqual(len(lines), rows)


class CliSurfaceTests(unittest.TestCase):
    def test_ui_command_is_registered_and_non_tty_safe(self):
        from code_mule.cli.composition import ProductionCliComposition

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project-state.json"
            JsonProjectStateStore(path).save(make_project_state())
            before = path.read_bytes()
            composition = ProductionCliComposition(
                path, environment={}, stdout=io.StringIO(), stderr=io.StringIO(),
                stdin=io.StringIO(),
            )
            demo = composition.ui(demo=True)
            self.assertEqual(
                demo.exit_code, CliExitCode.SUCCESS, "\n".join(demo.output)
            )
            self.assertTrue(any("HUMAN ACTION" in line for line in demo.output))
            live = composition.ui()
            self.assertEqual(live.exit_code, CliExitCode.SUCCESS)
            self.assertFalse(any("persistent UI unavailable" in line for line in live.output))
            self.assertEqual(path.read_bytes(), before)

    def test_phase22_renderers_are_unchanged(self):
        from code_mule.presentation.terminal import TerminalDashboard

        terminal = TerminalDashboard.for_stream(io.StringIO())
        paragraph = terminal.legacy(("CODE MULE", "Status  Running"))
        self.assertEqual(paragraph[0], "CODE MULE")
        self.assertEqual(paragraph[-1], "Status  Running")

    def test_persistent_ui_does_not_change_persisted_json(self):
        state = demo_state()
        before = serialize_project_state(state)
        control = controller(state=state)
        control.dispatch("status")
        control.dispatch("inspect --verbose")
        after = serialize_project_state(control.project_state())
        self.assertEqual(json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
