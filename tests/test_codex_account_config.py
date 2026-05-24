import tempfile
import unittest
from pathlib import Path

import genesis.config as config_mod
from genesis.repl import _rewrite_codex_accounts_text


class CodexAccountConfigTests(unittest.TestCase):
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
