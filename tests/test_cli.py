from __future__ import annotations

import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from genesis.main import main


class CLITests(unittest.TestCase):
    @patch("genesis.main._start_repl")
    def test_no_arguments_still_starts_interactive_repl(self, start_repl) -> None:
        self.assertEqual(0, main([]))
        start_repl.assert_called_once_with()

    @patch("genesis.main._with_repl")
    def test_plan_is_available_as_a_direct_command(self, with_repl) -> None:
        self.assertEqual(0, main(["plan", "make", "the", "harness", "faster"]))
        with_repl.assert_called_once_with("cmd_plan", "make the harness faster")

    @patch("genesis.main._with_repl")
    def test_existing_direct_commands_keep_their_argument_shapes(self, with_repl) -> None:
        cases = (
            (["status"], ("cmd_status",)),
            (["usage", "--REFRESH", "--JSON"], ("cmd_usage", ["--refresh", "--json"])),
            (["limits"], ("cmd_usage", [])),
            (["runs"], ("cmd_runs",)),
            (["resume", "run-7"], ("cmd_resume", "run-7")),
            (["retry", "run-7", "step-2"], ("cmd_retry", ["run-7", "step-2"])),
            (["cleanup", "run-7"], ("cmd_cleanup", "run-7")),
            (["inspect", "run-7"], ("cmd_inspect", "run-7")),
            (["memory", "search", "durable", "state"], ("cmd_memory", ["search", "durable", "state"])),
            (["config", "SHOW"], ("cmd_config", ["show"])),
        )

        for argv, expected in cases:
            with self.subTest(argv=argv):
                with_repl.reset_mock()
                self.assertEqual(0, main(argv))
                with_repl.assert_called_once_with(*expected)

    @patch("genesis.main._start_repl")
    @patch("genesis.main._with_repl")
    def test_unknown_command_is_an_error_and_never_opens_repl(self, with_repl, start_repl) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            exit_code = main(["rn", "do something"])

        self.assertEqual(2, exit_code)
        self.assertIn("unknown command 'rn'", stderr.getvalue())
        self.assertIn("genesis --help", stderr.getvalue())
        start_repl.assert_not_called()
        with_repl.assert_not_called()

    @patch("genesis.main._with_repl")
    @patch("genesis.main._run_task")
    def test_malformed_commands_return_usage_exit_code(self, run_task, with_repl) -> None:
        malformed = (
            ["run"],
            ["plan", "   "],
            ["status", "extra"],
            ["usage", "--unknown"],
            ["usage", "--json", "--json"],
            ["resume"],
            ["resume", "   "],
            ["resume", "one", "two"],
            ["retry", "run-only"],
            ["retry", "run", "   "],
            ["inspect"],
            ["cleanup", "one", "two"],
            ["memory", "search"],
            ["memory", "show", "extra"],
            ["config", "unknown"],
            ["--version", "extra"],
        )

        for argv in malformed:
            with self.subTest(argv=argv):
                stderr = StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(2, main(argv))
                self.assertIn("Error:", stderr.getvalue())
                self.assertIn("genesis --help", stderr.getvalue())

        run_task.assert_not_called()
        with_repl.assert_not_called()

    def test_help_is_rich_and_lists_direct_plan_command(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(0, main(["--help"]))

        output = stdout.getvalue()
        self.assertIn("GENESIS CLI", output)
        self.assertIn("plan <task>", output)
        self.assertIn("usage [--refresh] [--json]", output)
        self.assertIn("COMMANDS", output)
        self.assertIn("genesis [command] [arguments]", output)

    def test_python_module_propagates_usage_exit_status(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-m", "genesis", "not-a-real-command"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("unknown command 'not-a-real-command'", completed.stderr)


if __name__ == "__main__":
    unittest.main()
