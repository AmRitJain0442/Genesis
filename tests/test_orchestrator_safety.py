from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from genesis.agents.base import AgentInfo, BaseAgent
from genesis.agents.orchestrator import Orchestrator
from genesis.config import GenesisConfig
from genesis.git_ops import GitManager
from genesis.memory import MemoryManager
from genesis.palace import PalaceStore
from genesis.policy import ExecutionPolicy
from genesis.runtime import RuntimeStore
from genesis.worktree import WorktreeManager


class FakePlanReviewAgent(BaseAgent):
    def __init__(self, verdict: str = "rejected", task_id: str = "run-safe") -> None:
        super().__init__(AgentInfo("fake-orchestrator", "fake", "fake", 1000))
        self.verdict = verdict
        self.task_id = task_id

    def chat(self, system: str, messages: list[dict], output_callback=None) -> str:
        return self.chat_plan(system, messages)

    def chat_plan(self, system: str, messages: list[dict]) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "task_summary": "write file",
                "estimated_steps": 1,
                "steps": [
                    {
                        "step_id": "step-1",
                        "title": "Write file",
                        "description": "Create a file.",
                        "type": "code",
                        "preferred_agent": "any",
                        "depends_on": [],
                        "expected_output": "A file exists.",
                        "context_hint": "",
                    }
                ],
            }
        )

    def chat_review(self, system: str, messages: list[dict]) -> str:
        return json.dumps(
            {
                "step_id": "step-1",
                "verdict": self.verdict,
                "quality_score": 9 if self.verdict == "approved" else 2,
                "feedback": "" if self.verdict == "approved" else "The file is intentionally rejected.",
                "memory_note": "The worker wrote a file.",
                "should_retry": False,
                "suggested_revision": "",
            }
        )


class FakeWorkerAgent(BaseAgent):
    def __init__(
        self,
        filename: str = "bad.txt",
        content: str = "bad",
        files_by_step: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(AgentInfo("fake-worker", "fake", "fake", 1000))
        self.filename = filename
        self.content = content
        self.files_by_step = files_by_step or {}

    def chat(self, system: str, messages: list[dict], output_callback=None) -> str:
        prompt = messages[0].get("content", "") if messages else ""
        filename, content = self.filename, self.content
        for step_id, file_data in self.files_by_step.items():
            if f"Step ID: {step_id}" in prompt:
                filename, content = file_data
        return (
            "<result>\n"
            "Writing content.\n"
            f'<code lang="text" file="{filename}">\n'
            f"{content}\n"
            "</code>\n"
            "</result>"
        )


class OrchestratorSafetyTests(unittest.TestCase):
    def test_rejected_review_does_not_commit_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "README.md").write_text("initial\n", encoding="utf-8")
            self._git(root, "add", "README.md")
            self._git(root, "commit", "-m", "initial")
            before = self._commit_count(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)
            palace = PalaceStore(cfg.runtime.state_db)
            policy = ExecutionPolicy()

            orchestrator = Orchestrator(
                FakePlanReviewAgent(),
                {"fake-worker": FakeWorkerAgent()},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=palace,
                policy=policy,
            )

            orchestrator.run_task("write rejected file")

            self.assertEqual(before, self._commit_count(root))
            self.assertFalse((root / "bad.txt").exists())
            self.assertEqual("blocked", runtime.get_run("run-safe").status)
            step = runtime.get_step("run-safe", "step-1")
            self.assertEqual("blocked", step.status)
            self.assertTrue(step.patch_artifact_id)
            artifact = runtime.get_artifact(step.patch_artifact_id)
            self.assertIn("bad.txt", artifact.content)
            WorktreeManager(root).cleanup_run("run-safe")
            git.close()

    def test_approved_review_applies_patch_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            before = self._commit_count(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-ok"),
                {"fake-worker": FakeWorkerAgent(filename="ok.txt", content="ok")},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("write approved file")

            self.assertTrue((root / "ok.txt").exists())
            self.assertEqual("ok\n", (root / "ok.txt").read_text(encoding="utf-8"))
            self.assertGreater(self._commit_count(root), before)
            step = runtime.get_step("run-ok", "step-1")
            self.assertEqual("committed", step.status)
            self.assertTrue(step.commit_sha)
            git.close()

    def test_resume_skips_committed_steps_and_runs_pending_step(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)
            plan = {
                "task_id": "run-resume",
                "task_summary": "resume two step run",
                "estimated_steps": 2,
                "steps": [
                    {
                        "step_id": "step-1",
                        "title": "Already done",
                        "description": "Do not rerun this step.",
                        "type": "code",
                        "preferred_agent": "any",
                        "depends_on": [],
                        "expected_output": "First file.",
                        "context_hint": "",
                    },
                    {
                        "step_id": "step-2",
                        "title": "Pending step",
                        "description": "Create the pending file.",
                        "type": "code",
                        "preferred_agent": "any",
                        "depends_on": ["step-1"],
                        "expected_output": "Second file.",
                        "context_hint": "",
                    },
                ],
            }
            runtime.start_run("resume two step run", run_id="run-resume")
            runtime.checkpoint("run-resume", "plan_created", payload=plan)
            runtime.upsert_step("run-resume", "step-1", title="Already done", status="committed", commit_sha="abc1234")
            runtime.upsert_step("run-resume", "step-2", title="Pending step", status="pending")

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="unused"),
                {
                    "fake-worker": FakeWorkerAgent(
                        files_by_step={
                            "step-1": ("first.txt", "first"),
                            "step-2": ("second.txt", "second"),
                        }
                    )
                },
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.resume_task("run-resume")

            self.assertFalse((root / "first.txt").exists())
            self.assertTrue((root / "second.txt").exists())
            self.assertEqual("committed", runtime.get_step("run-resume", "step-1").status)
            self.assertEqual("committed", runtime.get_step("run-resume", "step-2").status)
            self.assertEqual("completed", runtime.get_run("run-resume").status)
            git.close()

    def _init_repo(self, root: Path) -> None:
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Test User")
        (root / "README.md").write_text("initial\n", encoding="utf-8")
        self._git(root, "add", "README.md")
        self._git(root, "commit", "-m", "initial")

    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

    def _commit_count(self, root: Path) -> int:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return int(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
