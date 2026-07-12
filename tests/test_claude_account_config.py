import tempfile
import unittest
from pathlib import Path

import genesis.config as config_mod
from genesis.repl import _rewrite_claude_accounts_text


class ClaudeAccountConfigTests(unittest.TestCase):
    def test_remove_one_claude_account_preserves_remaining_blocks(self) -> None:
        text = """\
[claude_cli]
command = "claude"
timeout = 300

[[claude_cli.accounts]]
name = "claude-main"
config_dir = ""
model = "auto"

# [[claude_cli.accounts]]
# name = "example"

[[claude_cli.accounts]]
name = "claude-2"
config_dir = "C:/Users/me/.claude-2"
model = "auto"
"""

        updated, removed, seen, remaining = _rewrite_claude_accounts_text(
            text,
            remove_names={"claude-main"},
        )

        self.assertEqual(["claude-main"], removed)
        self.assertEqual(["claude-main", "claude-2"], seen)
        self.assertEqual(["claude-2"], remaining)
        self.assertNotRegex(updated, r'(?m)^\s*name\s*=\s*"claude-main"')
        self.assertIn('name = "claude-2"', updated)
        self.assertIn("# [[claude_cli.accounts]]", updated)
        self.assertNotIn("accounts = []", updated)

    def test_remove_all_claude_accounts_writes_explicit_empty_marker(self) -> None:
        text = """\
[claude_cli]
command = "claude"
timeout = 300

[[claude_cli.accounts]]
name = "claude-main"
config_dir = ""
model = "auto"
"""

        updated, removed, _seen, remaining = _rewrite_claude_accounts_text(
            text,
            remove_all=True,
        )

        self.assertEqual(["claude-main"], removed)
        self.assertEqual([], remaining)
        self.assertIn("accounts = []", updated)
        self.assertNotRegex(updated, r"(?m)^\s*\[\[claude_cli\.accounts\]\]")

    def test_loader_marks_empty_accounts_as_explicit(self) -> None:
        original_file = config_mod.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """\
[claude_cli]
command = "claude"
timeout = 300
accounts = []
""",
                encoding="utf-8",
            )
            try:
                config_mod.CONFIG_FILE = path
                loaded = config_mod.load_config()
            finally:
                config_mod.CONFIG_FILE = original_file

        self.assertEqual([], loaded.claude_cli.accounts)
        self.assertTrue(loaded.claude_cli.accounts_explicit)

    def test_loader_parses_claude_accounts(self) -> None:
        original_file = config_mod.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """\
[claude_cli]
command = "claude"
timeout = 300

[[claude_cli.accounts]]
name = "claude-main"
config_dir = ""
model = "auto"

[[claude_cli.accounts]]
name = "claude-pro2"
config_dir = "C:/Users/me/.claude-pro2"
model = "auto"
""",
                encoding="utf-8",
            )
            try:
                config_mod.CONFIG_FILE = path
                loaded = config_mod.load_config()
            finally:
                config_mod.CONFIG_FILE = original_file

        names = [a.name for a in loaded.claude_cli.accounts]
        self.assertEqual(["claude-main", "claude-pro2"], names)
        self.assertEqual("C:/Users/me/.claude-pro2", loaded.claude_cli.accounts[1].config_dir)
        self.assertTrue(loaded.claude_cli.accounts_explicit)


if __name__ == "__main__":
    unittest.main()
