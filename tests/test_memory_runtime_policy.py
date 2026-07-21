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

    def test_manual_retry_clears_terminal_repair_state_but_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeStore(Path(td) / "state.db")
            run_id = store.start_run("repair feature", run_id="run-repair")
            store.update_run_status(
                run_id,
                "blocked",
                metadata={"blocked_steps": ["step-1"], "reason": "tests failed"},
            )
            store.upsert_step(
                run_id,
                "step-1",
                title="Repair feature",
                status="blocked",
                worker="worker-a",
                worktree_path="old-worktree",
                patch_artifact_id="old-patch",
                metadata={
                    "step": {"step_id": "step-1", "title": "Repair feature"},
                    "repair_attempts": 2,
                    "blocked_reason": "tests failed",
                    "block_kind": "repair_exhausted",
                    "lease": "blocked",
                    "retained_worktrees": ["older-draft", "oldest-draft"],
                    "integration_retry_from": "older-draft",
                },
            )

            store.reset_step_for_retry(run_id, "step-1")

            step = store.get_step(run_id, "step-1")
            self.assertEqual("pending", step.status)
            self.assertEqual("", step.worker)
            self.assertEqual("old-worktree", step.worktree_path)
            self.assertEqual("", step.patch_artifact_id)
            self.assertEqual(0, step.metadata["repair_attempts"])
            self.assertEqual(1, step.metadata["retry_generation"])
            self.assertEqual("step-1", step.metadata["step"]["step_id"])
            self.assertEqual(
                ["older-draft", "oldest-draft"],
                step.metadata["retained_worktrees"],
            )
            self.assertEqual(
                "older-draft", step.metadata["integration_retry_from"]
            )
            self.assertNotIn("blocked_reason", step.metadata)
            self.assertNotIn("block_kind", step.metadata)
            self.assertEqual([], store.get_run(run_id).metadata["blocked_steps"])
            retry_events = [
                event
                for event in store.events(run_id)
                if event.event_type == "manual_retry_started"
            ]
            self.assertEqual(1, len(retry_events))
            self.assertEqual("tests failed", retry_events[0].payload["prior_blocked_reason"])
            event_types = [event.event_type for event in store.events(run_id)]
            self.assertLess(
                event_types.index("manual_retry_started"),
                max(index for index, item in enumerate(event_types) if item == "step_status"),
            )

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
        self.assertEqual("test_exit", result.failure_kind)
        self.assertTrue(result.repairable)

        cfg.verification.commands = ["git reset --hard HEAD"]
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, policy, td).verify(changed_files=["src/app.py"])
        self.assertFalse(result.passed)
        self.assertEqual("policy_denied", result.failure_kind)
        self.assertFalse(result.repairable)

        cfg.verification.commands = [f'"{sys.executable}" -c "print(123)"']
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, policy, td).verify(changed_files=[".git/config"])
        self.assertFalse(result.passed)
        self.assertEqual("policy_denied", result.failure_kind)
        self.assertFalse(result.repairable)

        cfg.verification.commands = ["genesis-command-that-does-not-exist-91f85"]
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, policy, td).verify(changed_files=["src/app.py"])
        self.assertFalse(result.passed)
        self.assertEqual("spawn_error", result.failure_kind)
        self.assertFalse(result.repairable)

        cfg.verification.commands = [
            f'"{sys.executable}" -c "from pathlib import Path; '
            "Path(\'required-output.txt\').read_text()\""
        ]
        with tempfile.TemporaryDirectory() as td:
            result = Verifier(cfg, policy, td).verify(changed_files=["src/app.py"])
        self.assertFalse(result.passed)
        self.assertEqual("test_exit", result.failure_kind)
        self.assertTrue(result.repairable)


if __name__ == "__main__":
    unittest.main()
