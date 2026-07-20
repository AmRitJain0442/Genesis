from __future__ import annotations

import subprocess
import types
import unittest
from unittest import mock

import genesis.agents.base as base


class ProcessTreeCleanupTests(unittest.TestCase):
    def test_windows_taskkill_failure_falls_back_to_direct_kill(self) -> None:
        proc = mock.Mock(pid=1234)
        proc.poll.return_value = None
        failed = types.SimpleNamespace(returncode=1)

        with (
            mock.patch.object(base.os, "name", "nt"),
            mock.patch.object(base.subprocess, "run", return_value=failed) as run,
        ):
            base.terminate_process_tree(proc)

        run.assert_called_once()
        self.assertEqual(10, run.call_args.kwargs["timeout"])
        proc.kill.assert_called_once_with()

    def test_windows_taskkill_timeout_falls_back_to_direct_kill(self) -> None:
        proc = mock.Mock(pid=1234)
        proc.poll.return_value = None

        with (
            mock.patch.object(base.os, "name", "nt"),
            mock.patch.object(
                base.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("taskkill", 10),
            ),
        ):
            base.terminate_process_tree(proc)

        proc.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
