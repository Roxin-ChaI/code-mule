"""Fast, model-free smoke checks for the v0.2.0 release command surface."""

from __future__ import annotations

from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
import unittest

from code_mule import __version__
from code_mule.cli import CliCommandResult, CliExitCode, main
from code_mule.state import CURRENT_SCHEMA_VERSION, MIN_SUPPORTED_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]


class RecordingCommands:
    """Composition boundary that proves routing without constructing a model."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def command(*_args, **_kwargs):
            self.calls.append(name)
            return CliCommandResult(CliExitCode.SUCCESS, (f"{name}: smoke pass",))

        return command


class ReleaseCandidateSmokeTests(unittest.TestCase):
    def test_every_public_command_reaches_the_expected_typed_boundary(self):
        cases = (
            (("version", "--verbose"), "version"),
            (("doctor",), "doctor"),
            (("ui", "--demo"), "ui"),
            (("start", "--objective", "build it"), "start"),
            (("init", "--project-id", "p", "--name", "Project"), "init_project"),
            (("run", "--objective", "finish it"), "run"),
            (("status",), "status"),
            (("deliverable",), "deliverable"),
            (("launch",), "launch"),
            (("app-status",), "app_status"),
            (("stop-app",), "stop_app"),
            (("diagnose",), "diagnose"),
            (("ask", "what remains?"), "ask"),
            (("change", "add export"), "change"),
            (("change", "--apply"), "apply_change"),
            (("pause",), "pause"),
            (("resume",), "resume"),
            (("recover",), "recover"),
            (("stop",), "stop"),
            (("inspect",), "inspect"),
            (("approve", "action-1"), "approve"),
            (("reject", "action-1"), "reject"),
            (("answer", "action-1", "localStorage"), "answer"),
            (("resolve", "action-1", "--strategy", "acknowledge"), "resolve"),
            (("chat",), "chat"),
        )
        with TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                for arguments, expected in cases:
                    with self.subTest(command=arguments[0]):
                        commands = RecordingCommands()
                        output = StringIO()
                        errors = StringIO()

                        def factory(*_args):
                            return commands

                        code = main(
                            arguments,
                            composition_factory=factory,
                            environment={},
                            stdout=output,
                            stderr=errors,
                            stdin=StringIO(""),
                        )
                        self.assertEqual(code, CliExitCode.SUCCESS)
                        self.assertEqual(commands.calls, [expected])
                        self.assertEqual(errors.getvalue(), "")
                        self.assertIn("smoke pass", output.getvalue())
            finally:
                os.chdir(previous)

    def test_package_cli_and_schema_release_metadata_are_coherent(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], "0.2.0")
        self.assertEqual(__version__, project["project"]["version"])
        self.assertEqual(MIN_SUPPORTED_SCHEMA_VERSION, 1)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 16)

        output = StringIO()
        errors = StringIO()
        code = main(
            ("version", "--verbose"),
            environment={},
            stdout=output,
            stderr=errors,
            stdin=StringIO(""),
        )
        rendered = output.getvalue()
        self.assertEqual(code, CliExitCode.SUCCESS)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("Code Mule 0.2.0", rendered)
        self.assertIn("Schema support  1..16", rendered)


if __name__ == "__main__":
    unittest.main()
