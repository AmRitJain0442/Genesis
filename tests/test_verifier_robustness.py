from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest

from genesis.config import GenesisConfig
from genesis.policy import ExecutionPolicy
from genesis.verifier import Verifier


def _python_command(source: str) -> str:
    return subprocess.list2cmdline([sys.executable, "-c", source])


class VerifierRobustnessTests(unittest.TestCase):
    def test_large_output_is_bounded_and_keeps_head_and_tail(self) -> None:
        cfg = GenesisConfig()
        cfg.verification.commands = [
            _python_command("print('HEAD' + 'x' * 10000 + 'TAIL')")
        ]
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, ExecutionPolicy(), td).verify()

        self.assertTrue(result.passed)
        output = result.commands[0].output
        self.assertLessEqual(len(output.encode("utf-8")), 4000)
        self.assertTrue(output.startswith("HEAD"))
        self.assertTrue(output.endswith("TAIL"))
        self.assertIn("output truncated", output)

    def test_timeout_terminates_a_process_tree_without_pipe_hang(self) -> None:
        cfg = GenesisConfig()
        cfg.verification.timeout = 0.2
        cfg.verification.commands = [
            _python_command(
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "time.sleep(30)"
            )
        ]
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, ExecutionPolicy(), td).verify()
        elapsed = time.monotonic() - started

        self.assertFalse(result.passed)
        self.assertIn("timed out", result.reason)
        self.assertLess(elapsed, 10)

    def test_output_callback_failure_is_observer_only(self) -> None:
        cfg = GenesisConfig()
        cfg.verification.commands = [_python_command("print('ok')")]

        def broken_callback(_message: str) -> None:
            raise RuntimeError("UI failed")

        with tempfile.TemporaryDirectory() as td:
            result = Verifier(
                cfg,
                ExecutionPolicy(),
                td,
                output_callback=broken_callback,
            ).verify()

        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
