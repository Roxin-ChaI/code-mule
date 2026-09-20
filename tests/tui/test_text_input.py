"""End-to-end key decoding and Boss text-entry regression coverage."""

from __future__ import annotations

from dataclasses import replace
import io
import unittest

from code_mule.domain.enums import ProjectStatus
from code_mule.tui.app import _handle_key, read_key_event
from code_mule.tui.controller import TerminalController
from code_mule.tui.demo import DemoCommands
from code_mule.tui.keys import InputKind, control_event

from state import make_project_state


class FakeScreen:
    def __init__(self, key) -> None:
        self.key = key

    def get_wch(self):
        key, self.key = self.key, None
        return key


def controller(*, done: bool = False) -> tuple[TerminalController, DemoCommands]:
    state = make_project_state()
    if done:
        state = replace(
            state,
            project=replace(state.project, status=ProjectStatus.DONE),
            human_actions=(),
        )
    commands = DemoCommands()
    return (
        TerminalController(commands, lambda: state, input_stream=io.StringIO("")),
        commands,
    )


def type_text(control: TerminalController, text: str) -> None:
    for character in text:
        event = read_key_event(FakeScreen(character))
        assert event is not None
        assert _handle_key(control, event, rows=24, cols=80)


class BossTextEntryTests(unittest.TestCase):
    def test_common_commands_preserve_every_character(self):
        for command in ("status", "inspect", "deliverable"):
            with self.subTest(command=command):
                control, _commands = controller()
                type_text(control, command)
                self.assertEqual(control.buffered_input, command)

    def test_unicode_text_is_preserved(self):
        control, _commands = controller()
        type_text(control, "现在做到哪了")
        self.assertEqual(control.buffered_input, "现在做到哪了")

    def test_spaces_and_arguments_are_preserved(self):
        control, _commands = controller()
        type_text(control, "inspect --verbose")
        self.assertEqual(control.buffered_input, "inspect --verbose")

    def test_backspace_removes_the_actual_character(self):
        control, _commands = controller()
        type_text(control, "statusx")
        self.assertTrue(
            _handle_key(control, control_event("BACKSPACE"), rows=24, cols=80)
        )
        self.assertEqual(control.buffered_input, "status")

    def test_enter_dispatches_the_complete_text(self):
        control, commands = controller()
        type_text(control, "status")
        self.assertTrue(
            _handle_key(control, control_event("ENTER"), rows=24, cols=80)
        )
        control.join(timeout=5)
        self.assertEqual(commands.calls, ["status"])
        self.assertEqual(control.buffered_input, "")
        self.assertIn("me> status", [entry.text for entry in control.activity.entries])

    def test_arrow_and_control_keys_never_enter_the_buffer(self):
        control, _commands = controller()
        type_text(control, "stat")
        for name in ("KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN", "CTRL_L"):
            with self.subTest(name=name):
                _handle_key(control, control_event(name), rows=24, cols=80)
                self.assertEqual(control.buffered_input, "stat")

    def test_debug_classifies_text_without_replacing_it(self):
        control, _commands = controller()
        control.state.key_debug = True
        event = read_key_event(FakeScreen("s"))
        self.assertIsNotNone(event)
        self.assertIs(event.kind, InputKind.TEXT)
        self.assertEqual(event.key_name, "TEXT")
        self.assertEqual(event.text, "s")
        _handle_key(control, event, rows=24, cols=80)
        self.assertEqual(control.key_display, "TEXT")
        self.assertEqual(control.buffered_input, "s")

    def test_done_project_accepts_text_command_without_exiting(self):
        control, commands = controller(done=True)
        type_text(control, "status")
        _handle_key(control, control_event("ENTER"), rows=24, cols=80)
        control.join(timeout=5)
        self.assertEqual(commands.calls, ["status"])
        self.assertFalse(control.should_quit)
        self.assertEqual(control.project_state().project.status, ProjectStatus.DONE)


if __name__ == "__main__":
    unittest.main()
