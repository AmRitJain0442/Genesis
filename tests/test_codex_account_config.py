import tempfile
import unittest
from unittest import mock
from pathlib import Path

import genesis.config as config_mod
from genesis.config import CodexAccount, GenesisConfig
from genesis.repl import GenesisREPL
from genesis.repl import _rewrite_codex_accounts_text


class CodexAccountConfigTests(unittest.TestCase):
    def test_reserve_account_is_not_used_as_codex_brain(self) -> None:
        repl = GenesisREPL.__new__(GenesisREPL)
        repl.config = GenesisConfig()
        repl.work_dir = "."
        repl._orch_provider = "claude-cli"
        repl._worker_provider = "codex-cli"
        repl.config.codex_cli.accounts = [
            CodexAccount(
                name="CODEX-200",
                home="reserve-home",
                reserve=True,
            ),
            CodexAccount(
                name="terra",
                home="terra-home",
                model="gpt-5.6-terra",
            ),
        ]
        repl.config.codex_cli.accounts_explicit = True

        with (
            mock.patch("genesis.repl.find_claude_binary", return_value=None),
            mock.patch("genesis.repl.find_codex_binary", return_value="codex"),
        ):
            repl._build_agents()

        self.assertEqual("terra-home", repl._agents["codex-orchestrator"].codex_home)
        self.assertFalse(repl._agents["codex-orchestrator"].reserve)
        self.assertTrue(repl._agents["CODEX-200"].reserve)

    def test_loader_marks_reserve_account(self) -> None:
        original_file = config_mod.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """\
[[codex_cli.accounts]]
name = "terra"

[[codex_cli.accounts]]
name = "codex-200"
reserve = true
""",
                encoding="utf-8",
            )
            try:
                config_mod.CONFIG_FILE = path
                loaded = config_mod.load_config()
            finally:
                config_mod.CONFIG_FILE = original_file

        terra, reserve = loaded.codex_cli.accounts
        self.assertFalse(terra.reserve)
        self.assertTrue(reserve.reserve)

    def test_remove_one_codex_account_preserves_remaining_blocks(self) -> None:
        text = """\
[codex_cli]
command = "codex"
timeout = 600

[[codex_cli.accounts]]
name = "codex-main"
home = ""
model = "auto"

# [[codex_cli.accounts]]
# name = "example"

[[codex_cli.accounts]]
name = "codex-2"
home = "C:/Users/me/.codex-2"
model = "auto"
"""

        updated, removed, seen, remaining = _rewrite_codex_accounts_text(
            text,
            remove_names={"codex-main"},
        )

        self.assertEqual(["codex-main"], removed)
        self.assertEqual(["codex-main", "codex-2"], seen)
        self.assertEqual(["codex-2"], remaining)
        self.assertNotRegex(updated, r'(?m)^\s*name\s*=\s*"codex-main"')
        self.assertIn('name = "codex-2"', updated)
        self.assertIn("# [[codex_cli.accounts]]", updated)
        self.assertNotIn("accounts = []", updated)

    def test_remove_all_codex_accounts_writes_explicit_empty_marker(self) -> None:
        text = """\
[codex_cli]
command = "codex"
timeout = 600

[[codex_cli.accounts]]
name = "codex-main"
home = ""
model = "auto"
"""

        updated, removed, _seen, remaining = _rewrite_codex_accounts_text(
            text,
            remove_all=True,
        )

        self.assertEqual(["codex-main"], removed)
        self.assertEqual([], remaining)
        self.assertIn("accounts = []", updated)
        self.assertNotRegex(updated, r"(?m)^\s*\[\[codex_cli\.accounts\]\]")

    def test_loader_marks_empty_accounts_as_explicit(self) -> None:
        original_file = config_mod.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """\
[codex_cli]
command = "codex"
timeout = 600
accounts = []
""",
                encoding="utf-8",
            )
            try:
                config_mod.CONFIG_FILE = path
                loaded = config_mod.load_config()
            finally:
                config_mod.CONFIG_FILE = original_file

        self.assertEqual([], loaded.codex_cli.accounts)
        self.assertTrue(loaded.codex_cli.accounts_explicit)


if __name__ == "__main__":
    unittest.main()
