import unittest
from unittest import mock

from genesis.agents.base import AgentInfo
from genesis.agents.codex_cli import CodexCLIAgent, _InactivityWatchdog


class SandboxFlagTests(unittest.TestCase):
    def test_windows_write_bypasses_unenforceable_sandbox(self):
        with mock.patch("genesis.agents.codex_cli.os.name", "nt"):
            flags = CodexCLIAgent._sandbox_flags(True)
        self.assertEqual(["--dangerously-bypass-approvals-and-sandbox"], flags)
        # never silently falls back to a mode that reads as read-only on Windows
        self.assertNotIn("workspace-write", flags)

    def test_posix_write_keeps_real_sandbox(self):
        with mock.patch("genesis.agents.codex_cli.os.name", "posix"):
            flags = CodexCLIAgent._sandbox_flags(True)
        self.assertIn("--sandbox", flags)
        self.assertIn("workspace-write", flags)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", flags)

    def test_read_only_is_sandboxed_on_every_platform(self):
        for name in ("nt", "posix"):
            with mock.patch("genesis.agents.codex_cli.os.name", name):
                flags = CodexCLIAgent._sandbox_flags(False)
            self.assertIn("read-only", flags)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", flags)

    def test_reserve_worker_keeps_full_access_in_its_worktree_copy(self):
        agent = CodexCLIAgent(
            AgentInfo("codex-200", "codex-cli", "auto", max_tokens=8096),
            reserve=True,
        )

        worker = agent.for_work_dir(".")

        self.assertTrue(worker.reserve)
        with mock.patch("genesis.agents.codex_cli.os.name", "nt"):
            self.assertEqual(
                ["--dangerously-bypass-approvals-and-sandbox"],
                worker._sandbox_flags(True),
            )


class InactivityWatchdogTests(unittest.TestCase):
    def test_activity_replaces_deadline_and_stale_timer_cannot_fire(self):
        timers = []
        fired = []

        class FakeTimer:
            def __init__(self, timeout, callback):
                self.timeout = timeout
                self.callback = callback
                self.cancelled = False
                self.started = False
                self.daemon = False
                timers.append(self)

            def start(self):
                self.started = True

            def cancel(self):
                self.cancelled = True

        watchdog = _InactivityWatchdog(
            600,
            lambda: fired.append(True),
            timer_factory=FakeTimer,
        )
        watchdog.start()
        first = timers[-1]
        watchdog.touch()
        second = timers[-1]

        self.assertTrue(first.cancelled)
        self.assertTrue(second.started)
        first.callback()
        self.assertEqual([], fired)
        second.callback()
        self.assertEqual([True], fired)


if __name__ == "__main__":
    unittest.main()
