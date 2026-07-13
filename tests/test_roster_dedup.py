import json
import os
import tempfile
import types
import unittest

from genesis.repl import GenesisREPL


def _codex(name, home):
    return types.SimpleNamespace(name=name, provider="codex-cli", model="auto",
                                 codex_home=home)


def _claude(name, config_dir):
    return types.SimpleNamespace(name=name, provider="claude-cli",
                                 model="claude-sonnet-4-6", config_dir=config_dir)


class AgentIdentityTests(unittest.TestCase):
    def test_same_config_dir_shares_identity(self) -> None:
        a = _claude("claude-cli-orchestrator", "C:/x/.claude-a")
        b = _claude("account-a", "C:/x/.claude-a")
        self.assertEqual(GenesisREPL._agent_identity(a), GenesisREPL._agent_identity(b))

    def test_different_config_dirs_differ(self) -> None:
        a = _claude("account-a", "C:/x/.claude-a")
        b = _claude("account-b", "C:/x/.claude-b")
        self.assertNotEqual(GenesisREPL._agent_identity(a), GenesisREPL._agent_identity(b))

    def test_codex_same_home_shares_identity(self) -> None:
        a = _codex("codex-orchestrator", "C:/x/.codex-a")
        b = _codex("account-a", "C:/x/.codex-a")
        self.assertEqual(GenesisREPL._agent_identity(a), GenesisREPL._agent_identity(b))

    def test_codex_same_account_id_across_homes_collapses(self) -> None:
        GenesisREPL._LOGIN_ID_CACHE.clear()
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            for d in (d1, d2):
                with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as f:
                    json.dump({"tokens": {"account_id": "same-id-123"}}, f)
            a = _codex("codex-main", d1)
            b = _codex("codex-dup", d2)
            self.assertEqual(GenesisREPL._agent_identity(a), GenesisREPL._agent_identity(b))
            self.assertTrue(GenesisREPL._agent_identity(a).endswith("same-id-123"))

    def test_codex_distinct_account_ids_stay_separate(self) -> None:
        GenesisREPL._LOGIN_ID_CACHE.clear()
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            for d, acct in ((d1, "id-1"), (d2, "id-2")):
                with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as f:
                    json.dump({"tokens": {"account_id": acct}}, f)
            a = _codex("codex-a", d1)
            b = _codex("codex-b", d2)
            self.assertNotEqual(GenesisREPL._agent_identity(a), GenesisREPL._agent_identity(b))

    def test_missing_auth_falls_back_to_path(self) -> None:
        GenesisREPL._LOGIN_ID_CACHE.clear()
        with tempfile.TemporaryDirectory() as d:
            a = _codex("codex-a", d)
            self.assertIsNone(GenesisREPL._codex_login_id(d))
            self.assertEqual("codex:" + os.path.normcase(os.path.abspath(d)),
                             GenesisREPL._agent_identity(a))


if __name__ == "__main__":
    unittest.main()
