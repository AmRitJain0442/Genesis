from __future__ import annotations

import json
import subprocess
import tempfile
import time
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
from genesis.schemas.plan import Plan
from genesis.worktree import WorktreeManager


class FakePlanReviewAgent(BaseAgent):
    def __init__(
        self,
        verdict: str = "rejected",
        task_id: str = "run-safe",
        steps: list[dict] | None = None,
        verdicts_by_step: dict[str, str] | None = None,
        verdict_sequences_by_step: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(AgentInfo("fake-orchestrator", "fake", "fake", 1000))
        self.verdict = verdict
        self.task_id = task_id
        self.steps = steps or [
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
        ]
        self.verdicts_by_step = verdicts_by_step or {}
        self.verdict_sequences_by_step = {
            step_id: list(verdicts)
            for step_id, verdicts in (verdict_sequences_by_step or {}).items()
        }
        self.review_counts: dict[str, int] = {}
        self.plan_calls = 0

    def chat(self, system: str, messages: list[dict], output_callback=None) -> str:
        return self.chat_plan(system, messages)

    def chat_plan(self, system: str, messages: list[dict]) -> str:
        self.plan_calls += 1
        return json.dumps(
            {
                "task_id": self.task_id,
                "task_summary": "write file",
                "estimated_steps": len(self.steps),
                "steps": self.steps,
            }
        )

    def chat_review(self, system: str, messages: list[dict]) -> str:
        prompt = messages[0].get("content", "") if messages else ""
        step_id = "step-1"
        for line in prompt.splitlines():
            if line.strip().startswith("ID: "):
                step_id = line.split("ID: ", 1)[1].strip()
                break
        self.review_counts[step_id] = self.review_counts.get(step_id, 0) + 1
        sequence = self.verdict_sequences_by_step.get(step_id)
        if sequence:
            verdict = sequence.pop(0)
        else:
            verdict = self.verdicts_by_step.get(step_id, self.verdict)
        return json.dumps(
            {
                "step_id": step_id,
                "verdict": verdict,
                "quality_score": 9 if verdict == "approved" else 2,
                "feedback": "" if verdict == "approved" else "The file is intentionally rejected.",
                "memory_note": "The worker wrote a file.",
                "should_retry": verdict == "needs_revision",
                "suggested_revision": "Fix the file." if verdict == "needs_revision" else "",
            }
        )


class FakeWorkerAgent(BaseAgent):
    def __init__(
        self,
        filename: str = "bad.txt",
        content: str = "bad",
        files_by_step: dict[str, tuple[str, str]] | None = None,
        repair_files_by_step: dict[str, tuple[str, str]] | None = None,
        delay_by_step: dict[str, float] | None = None,
    ) -> None:
        super().__init__(AgentInfo("fake-worker", "fake", "fake", 1000))
        self.filename = filename
        self.content = content
        self.files_by_step = files_by_step or {}
        self.repair_files_by_step = repair_files_by_step or {}
        self.delay_by_step = delay_by_step or {}

    def chat(self, system: str, messages: list[dict], output_callback=None) -> str:
        prompt = messages[0].get("content", "") if messages else ""
        filename, content = self.filename, self.content
        for step_id, file_data in self.files_by_step.items():
            if f"Step ID: {step_id}" in prompt:
                filename, content = file_data
                if "REVISION REQUIRED:" in prompt and step_id in self.repair_files_by_step:
                    filename, content = self.repair_files_by_step[step_id]
                if step_id in self.delay_by_step:
                    time.sleep(self.delay_by_step[step_id])
        for step_id, file_data in self.repair_files_by_step.items():
            if "REVISION REQUIRED:" in prompt and f"Step ID: {step_id}" in prompt:
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
    def test_saved_plan_preview_is_reused_when_task_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            planner = FakePlanReviewAgent(verdict="approved", task_id="run-retained")
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"fake-worker": FakeWorkerAgent(filename="saved.txt", content="saved")},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            first = orchestrator.plan_and_save("retain this exact plan")
            second = orchestrator.plan_and_save("  RETAIN THIS EXACT PLAN  ")
            orchestrator.run_task("retain this exact plan")
            git.close()

            self.assertEqual(first, second)
            self.assertEqual(1, planner.plan_calls)
            self.assertEqual("saved\n", (root / "saved.txt").read_text(encoding="utf-8"))
            self.assertEqual("completed", runtime.get_run("run-retained").status)

    def test_repeating_blocked_task_retries_retained_plan_without_replanning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            planner = FakePlanReviewAgent(
                task_id="run-blocked-reuse",
                verdict_sequences_by_step={"step-1": ["rejected", "approved"]},
            )
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"fake-worker": FakeWorkerAgent(filename="retried.txt", content="ok")},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("retry this retained task")
            first_status = runtime.get_run("run-blocked-reuse").status
            orchestrator.run_task("retry this retained task")
            git.close()

            self.assertEqual("blocked", first_status)
            self.assertEqual(1, planner.plan_calls)
            self.assertEqual("ok\n", (root / "retried.txt").read_text(encoding="utf-8"))
            self.assertEqual("completed", runtime.get_run("run-blocked-reuse").status)

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

    def test_deterministic_gate_blocks_missing_artifacts_before_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-gated",
                steps=[{
                    "step_id": "step-1",
                    "title": "Harden configuration",
                    "description": (
                        "Remove the hardcoded API key. Create a real .env.example and "
                        "add requirements.txt with pinned dependencies."
                    ),
                    "type": "config",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "expected_output": "Required artifacts exist and secrets are removed.",
                    "context_hint": "",
                }],
            )
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"fake-worker": FakeWorkerAgent(filename="SECURITY.md", content="all clean")},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("harden configuration")

            self.assertEqual({}, planner.review_counts)
            self.assertEqual("blocked", runtime.get_run("run-gated").status)
            step = runtime.get_step("run-gated", "step-1")
            self.assertIn("acceptance gates failed", step.metadata["blocked_reason"].lower())
            gate_events = [
                event for event in runtime.events("run-gated")
                if event.event_type == "deterministic_gates"
            ]
            self.assertEqual(1, len(gate_events))
            self.assertFalse(gate_events[0].payload["passed"])
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
            event_types = [event.event_type for event in runtime.events("run-ok")]
            self.assertIn("review_completed", event_types)
            self.assertIn("verification_completed", event_types)
            self.assertIn("release_summary", event_types)
            git.close()

    def test_dirty_worktree_is_checkpointed_before_isolated_execution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            (root / "README.md").write_text("local tracked work\n", encoding="utf-8")
            (root / "draft.txt").write_text("local untracked work\n", encoding="utf-8")

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)
            statuses: list[str] = []

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-dirty"),
                {"fake-worker": FakeWorkerAgent(filename="ok.txt", content="ok")},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task(
                "work from local changes",
                callbacks={"on_status": statuses.append},
            )

            subjects = self._git_output(root, "log", "--pretty=%s").splitlines()
            checkpoint = next(
                subject for subject in subjects if subject.startswith("[genesis] checkpoint:")
            )
            self.assertIn("preserve worktree", checkpoint)
            checkpoint_sha = self._git_output(
                root, "log", "--format=%H", "--grep=^\\[genesis\\] checkpoint:"
            ).splitlines()[0]
            self.assertEqual(
                "local tracked work\n",
                self._git_output(root, "show", f"{checkpoint_sha}:README.md"),
            )
            self.assertEqual(
                "local untracked work\n",
                self._git_output(root, "show", f"{checkpoint_sha}:draft.txt"),
            )
            self.assertEqual("ok\n", (root / "ok.txt").read_text(encoding="utf-8"))
            self.assertTrue(any("Saved current project state" in item for item in statuses))
            self.assertEqual("completed", runtime.get_run("run-dirty").status)
            git.close()

    def test_review_retry_records_repair_and_reviewer_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)

            reviewer = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-review-repair",
                verdict_sequences_by_step={"step-1": ["needs_revision", "approved"]},
            )
            orchestrator = Orchestrator(
                reviewer,
                {
                    "fake-worker": FakeWorkerAgent(
                        filename="ok.txt",
                        content="first",
                        repair_files_by_step={"step-1": ("ok.txt", "fixed")},
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

            orchestrator.run_task("repair after review")

            step = runtime.get_step("run-review-repair", "step-1")
            self.assertEqual("committed", step.status)
            self.assertEqual("fake-orchestrator", step.metadata.get("reviewer"))
            self.assertEqual("approved", step.metadata.get("review_verdict"))
            self.assertEqual(1, step.metadata.get("repair_attempts"))
            self.assertEqual("committed", step.metadata.get("review_state"))
            self.assertEqual(
                step.metadata.get("current_patch_sha"),
                step.metadata.get("reviewed_patch_sha"),
            )
            events = runtime.events("run-review-repair")
            event_types = [event.event_type for event in events]
            self.assertIn("repair_attempted", event_types)
            self.assertIn("review_superseded", event_types)
            versions = [
                event.payload for event in events
                if event.event_type == "patch_version_captured"
            ]
            reviews = [
                event.payload for event in events
                if event.event_type == "review_completed"
            ]
            self.assertEqual([1, 2], [item["patch_version"] for item in versions])
            self.assertEqual([1, 2], [item["patch_version"] for item in reviews])
            self.assertNotEqual(reviews[0]["patch_sha"], reviews[1]["patch_sha"])
            self.assertEqual("fixed\n", (root / "ok.txt").read_text(encoding="utf-8"))
            git.close()

    def test_verification_failure_can_be_repaired_with_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = [
                "python -c \"from pathlib import Path; raise SystemExit(Path('ok.txt').read_text().strip() != 'fixed')\""
            ]
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-verify-repair"),
                {
                    "fake-worker": FakeWorkerAgent(
                        filename="ok.txt",
                        content="bad",
                        repair_files_by_step={"step-1": ("ok.txt", "fixed")},
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

            orchestrator.run_task("repair after verification")

            self.assertEqual("fixed\n", (root / "ok.txt").read_text(encoding="utf-8"))
            step = runtime.get_step("run-verify-repair", "step-1")
            self.assertEqual("committed", step.status)
            self.assertEqual(1, step.metadata.get("repair_attempts"))
            event_types = [event.event_type for event in runtime.events("run-verify-repair")]
            self.assertIn("repair_attempted", event_types)
            self.assertIn("verification_completed", event_types)
            git.close()

    def test_advisory_verification_commits_despite_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            before = self._commit_count(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 0
            cfg.verification.commands = ['python -c "raise SystemExit(1)"']
            cfg.verification.require_for_commit = False
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-advisory"),
                {"fake-worker": FakeWorkerAgent(filename="ok.txt", content="ok")},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("commit despite failing verification")

            # Verification fails, but require_for_commit=false makes it advisory.
            self.assertTrue((root / "ok.txt").exists())
            self.assertGreater(self._commit_count(root), before)
            step = runtime.get_step("run-advisory", "step-1")
            self.assertEqual("committed", step.status)
            self.assertTrue(step.commit_sha)
            git.close()

    def test_required_verification_blocks_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            before = self._commit_count(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 0
            cfg.verification.commands = ['python -c "raise SystemExit(1)"']
            cfg.verification.require_for_commit = True
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-required"),
                {"fake-worker": FakeWorkerAgent(filename="ok.txt", content="ok")},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("block on failing verification")

            # Default gate: a failing verification must block the commit.
            self.assertEqual(before, self._commit_count(root))
            step = runtime.get_step("run-required", "step-1")
            self.assertEqual("blocked", step.status)
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

    def test_independent_steps_can_use_parallel_worker_leases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.max_parallel_workers = 2
            cfg.verification.commands = []
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(cfg.runtime.state_db)
            steps = [
                {
                    "step_id": "step-1",
                    "title": "Write A",
                    "description": "Create alpha.txt.",
                    "type": "code",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "expected_output": "alpha.txt exists.",
                    "context_hint": "alpha.txt",
                },
                {
                    "step_id": "step-2",
                    "title": "Write B",
                    "description": "Create beta.txt.",
                    "type": "code",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "expected_output": "beta.txt exists.",
                    "context_hint": "beta.txt",
                },
            ]

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-parallel", steps=steps),
                {
                    "fake-worker-a": FakeWorkerAgent(
                        files_by_step={
                            "step-1": ("alpha.txt", "alpha"),
                            "step-2": ("beta.txt", "beta"),
                        },
                        delay_by_step={"step-1": 0.05},
                    ),
                    "fake-worker-b": FakeWorkerAgent(
                        files_by_step={
                            "step-1": ("alpha.txt", "alpha"),
                            "step-2": ("beta.txt", "beta"),
                        },
                    ),
                },
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("write independent files")

            self.assertEqual("alpha\n", (root / "alpha.txt").read_text(encoding="utf-8"))
            self.assertEqual("beta\n", (root / "beta.txt").read_text(encoding="utf-8"))
            step_1 = runtime.get_step("run-parallel", "step-1")
            step_2 = runtime.get_step("run-parallel", "step-2")
            self.assertEqual("committed", step_1.status)
            self.assertEqual("committed", step_2.status)
            self.assertEqual("released", step_1.metadata.get("lease"))
            self.assertEqual("released", step_2.metadata.get("lease"))
            self.assertEqual({"fake-worker-a", "fake-worker-b"}, {step_1.worker, step_2.worker})
            self.assertEqual("completed", runtime.get_run("run-parallel").status)
            git.close()

    def test_retry_scope_includes_downstream_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            git = GitManager(str(root), cfg.git)
            runtime = RuntimeStore(root / ".genesis" / "state" / "state.db")
            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved"),
                {"fake-worker": FakeWorkerAgent()},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(root / ".genesis" / "state" / "state.db"),
                policy=ExecutionPolicy(),
            )
            plan = FakePlanReviewAgent(
                steps=[
                    {
                        "step_id": "step-1",
                        "title": "Base",
                        "description": "Base.",
                        "type": "code",
                        "preferred_agent": "any",
                        "depends_on": [],
                        "expected_output": "Done.",
                        "context_hint": "base.txt",
                    },
                    {
                        "step_id": "step-2",
                        "title": "Middle",
                        "description": "Middle.",
                        "type": "code",
                        "preferred_agent": "any",
                        "depends_on": ["step-1"],
                        "expected_output": "Done.",
                        "context_hint": "middle.txt",
                    },
                    {
                        "step_id": "step-3",
                        "title": "Leaf",
                        "description": "Leaf.",
                        "type": "code",
                        "preferred_agent": "any",
                        "depends_on": ["step-2"],
                        "expected_output": "Done.",
                        "context_hint": "leaf.txt",
                    },
                ]
            )
            parsed = orchestrator._extract_json(plan.chat_plan("", []))

            self.assertEqual(["step-2", "step-3"], orchestrator._retry_step_ids(Plan(**parsed), "step-2"))
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
        return int(self._git_output(root, "rev-list", "--count", "HEAD").strip())

    def _git_output(self, root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout


if __name__ == "__main__":
    unittest.main()
