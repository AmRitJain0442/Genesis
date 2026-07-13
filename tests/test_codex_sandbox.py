import unittest
from unittest import mock

from genesis.agents.codex_cli import CodexCLIAgent


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


if __name__ == "__main__":
    unittest.main()
