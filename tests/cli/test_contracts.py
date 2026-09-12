import unittest

from code_mule.cli import (
    CliError,
    CliExitCode,
    CliHumanActionRequired,
    InvalidCliProjectState,
    build_parser,
)


class CliContractTests(unittest.TestCase):
    def test_exit_codes_are_stable(self):
        self.assertEqual(int(CliExitCode.SUCCESS), 0)
        self.assertEqual(int(CliExitCode.INVALID_USAGE), 2)
        self.assertEqual(int(CliExitCode.INVALID_PROJECT_STATE), 3)
        self.assertEqual(int(CliExitCode.HUMAN_ACTION_REQUIRED), 4)
        self.assertEqual(int(CliExitCode.PROVIDER_OR_WORKER_FAILURE), 5)
        self.assertEqual(int(CliExitCode.ENVIRONMENT_CHECK_FAILED), 6)
        self.assertEqual(
            InvalidCliProjectState("invalid").exit_code,
            CliExitCode.INVALID_PROJECT_STATE,
        )
        self.assertEqual(
            CliHumanActionRequired("human").exit_code,
            CliExitCode.HUMAN_ACTION_REQUIRED,
        )
        with self.assertRaises(ValueError):
            CliError("")

    def test_parser_exposes_all_boss_commands(self):
        parser = build_parser()
        cases = (
            (["doctor"], "doctor"),
            (["doctor", "--verbose"], "doctor"),
            (["start", "--objective", "build it"], "start"),
            (["start"], "start"),
            (["init", "--project-id", "p", "--name", "Project"], "init"),
            (["run", "--objective", "build it"], "run"),
            (["status"], "status"),
            (["deliverable"], "deliverable"),
            (["launch"], "launch"),
            (["app-status"], "app-status"),
            (["stop-app"], "stop-app"),
            (["diagnose"], "diagnose"),
            (["diagnose", "--verbose"], "diagnose"),
            (["ask", "what remains?"], "ask"),
            (["change", "add multiply"], "change"),
            (["change", "--apply"], "change"),
            (["pause"], "pause"),
            (["resume"], "resume"),
            (["recover"], "recover"),
            (["recover", "--verbose"], "recover"),
            (["stop"], "stop"),
            (["inspect"], "inspect"),
            (["approve", "action-1"], "approve"),
            (["reject", "action-1"], "reject"),
            (["answer", "action-1", "Use SQLite"], "answer"),
            (["resolve", "action-1", "--strategy", "acknowledge"], "resolve"),
            (["chat"], "chat"),
            (["chat", "--verbose"], "chat"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(parser.parse_args(arguments).command, expected)

    def test_argparse_rejects_missing_command_with_usage_code(self):
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args([])
        self.assertEqual(raised.exception.code, CliExitCode.INVALID_USAGE)


if __name__ == "__main__":
    unittest.main()
