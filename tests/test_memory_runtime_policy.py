from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from genesis.config import GenesisConfig
from genesis.palace import PalaceStore
from genesis.policy import ExecutionPolicy
from genesis.runtime import RuntimeStore
from genesis.verifier import Verifier


class MemoryRuntimePolicyTests(unittest.TestCase):
    def test_palace_store_keeps_verbatim_searchable_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = PalaceStore(Path(td) / "state.db")
            store.add_drawer(
                wing="repo",
                room="run-1",
                closet="auth",
                kind="decision",
                title="Auth middleware decision",
                content="Use signed session cookies for the auth middleware.",
                source="test",
            )

            hits = store.search("session cookies", wing="repo")

            self.assertEqual(1, len(hits))
            self.assertEqual("Auth middleware decision", hits[0].title)
            self.assertIn("signed session cookies", hits[0].content)
            self.assertIn("Auth middleware decision", store.wakeup_context("auth", wing="repo"))

    def test_runtime_store_records_runs_checkpoints_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeStore(Path(td) / "state.db")
            run_id = store.start_run("build feature", run_id="run-a")
            store.checkpoint(run_id, "planned", step_id="step-1", payload={"ok": True})
            store.update_run_status(run_id, "completed", metadata={"steps": 1})

            run = store.get_run(run_id)
            events = store.events(run_id)

            self.assertIsNotNone(run)
            self.assertEqual("completed", run.status)
            self.assertTrue(any(e.event_type == "checkpoint" for e in events))
            self.assertTrue(any(e.event_type == "run_completed" for e in events))

    def test_policy_and_verifier_block_unsafe_actions(self) -> None:
        cfg = GenesisConfig()
        policy = ExecutionPolicy()

        self.assertFalse(policy.check_paths([".git/config"]).allowed)
        self.assertFalse(policy.check_command("git reset --hard HEAD").allowed)

        cfg.verification.commands = [f'"{sys.executable}" -c "print(123)"']
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, policy, td).verify(changed_files=["src/app.py"])
        self.assertTrue(result.passed)

        cfg.verification.commands = [f'"{sys.executable}" -c "import sys; sys.exit(3)"']
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, policy, td).verify(changed_files=["src/app.py"])
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
