from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        retry_override: bool | None = None,
        review_step_id: str | None = None,
        dialogue_actions: list[tuple[str, str]] | None = None,
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
        self.retry_override = retry_override
        self.review_step_id = review_step_id
        self.dialogue_actions = list(dialogue_actions or [])
        self.dialogue_calls = 0

    def chat(self, system: str, messages: list[dict], output_callback=None) -> str:
        if "brain directing a worker" in system:
            self.dialogue_calls += 1
            action, feedback = (
                self.dialogue_actions.pop(0)
                if self.dialogue_actions
                else ("approve", "")
            )
            return json.dumps({"action": action, "feedback": feedback})
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
                "step_id": self.review_step_id or step_id,
                "verdict": verdict,
                "quality_score": 9 if verdict == "approved" else 2,
                "feedback": "" if verdict == "approved" else "The file is intentionally rejected.",
                "memory_note": "The worker wrote a file.",
                "should_retry": (
                    self.retry_override
                    if self.retry_override is not None
                    else verdict == "needs_revision"
                ),
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


class ProgressWorkerAgent(FakeWorkerAgent):
    def chat(self, system: str, messages: list[dict], output_callback=None) -> str:
        if output_callback:
            output_callback("worker progress")
        return super().chat(system, messages, output_callback=output_callback)


def _worker_response(files: dict[str, str] | None = None, text: str = "Working.") -> str:
    blocks = "\n".join(
        f'<code lang="text" file="{path}">\n{content}\n</code>'
        for path, content in (files or {}).items()
    )
    return f"<result>\n{text}\n{blocks}\n</result>"


class ScriptedWorkerAgent(BaseAgent):
    """Worker fake with deterministic responses for repair-state tests."""

    def __init__(self, turns: list[str | Exception]) -> None:
        super().__init__(AgentInfo("scripted-worker", "fake", "fake", 1000))
        self.turns = list(turns)
        self.prompts: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    def chat(self, system: str, messages: list[dict], output_callback=None) -> str:
        self.prompts.append(messages[0].get("content", "") if messages else "")
        if not self.turns:
            raise AssertionError("scripted worker received an unexpected extra turn")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


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
            cfg.runtime.retry_budget = 0
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

    def test_acceptance_failure_repairs_before_review_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-gate-repair",
                steps=[{
                    "step_id": "step-1",
                    "title": "Pin dependencies",
                    "description": "Create requirements.txt with pinned dependencies.",
                    "type": "config",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "expected_output": "Runtime dependencies are exactly pinned.",
                    "context_hint": "",
                }],
            )
            worker = ScriptedWorkerAgent([
                _worker_response({"SECURITY.md": "draft"}),
                _worker_response({"requirements.txt": "requests==2.32.3"}),
            ])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            errors: list[str] = []
            repair_callbacks: list[dict] = []
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task(
                "pin dependencies",
                callbacks={
                    "on_error": lambda step, reason: errors.append(reason),
                    "on_repair": lambda step, event: repair_callbacks.append(event),
                },
            )

            self.assertEqual(2, worker.call_count)
            self.assertEqual(1, planner.review_counts.get("step-1"))
            self.assertEqual([], errors)
            self.assertEqual("completed", runtime.get_run("run-gate-repair").status)
            self.assertEqual(
                "requests==2.32.3\n",
                (root / "requirements.txt").read_text(encoding="utf-8"),
            )
            events = runtime.events("run-gate-repair", limit=200)
            gates = [
                event.payload["passed"]
                for event in events
                if event.event_type == "deterministic_gates"
            ]
            self.assertEqual([False, True], gates)
            repairs = [
                event.payload
                for event in events
                if event.event_type == "repair_attempted"
            ]
            self.assertEqual(1, len(repairs))
            self.assertEqual("acceptance", repairs[0]["stage"])
            self.assertEqual(0, repairs[0]["attempts_left"])
            self.assertEqual(1, len(repair_callbacks))
            self.assertEqual(repairs[0]["repair_id"], repair_callbacks[0]["repair_id"])
            step = runtime.get_step("run-gate-repair", "step-1")
            self.assertEqual("committed", step.status)
            self.assertEqual(1, step.metadata.get("repair_attempts"))
            self.assertEqual(
                step.metadata.get("current_patch_sha"),
                step.metadata.get("reviewed_patch_sha"),
            )
            self.assertIn("REVISION REQUIRED:", worker.prompts[1])
            self.assertIn("requirements.txt", worker.prompts[1])
            git.close()

    def test_empty_patch_is_repaired_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            worker = ScriptedWorkerAgent([
                _worker_response(text="I inspected the task but changed nothing."),
                _worker_response({"fixed.txt": "fixed"}),
            ])
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-empty-repair",
            )
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            output: list[str] = []

            orchestrator.run_task(
                "repair an empty worker result",
                callbacks={"on_output": output.append},
            )

            self.assertEqual(2, worker.call_count)
            self.assertEqual(1, planner.review_counts.get("step-1"))
            self.assertEqual("fixed\n", (root / "fixed.txt").read_text(encoding="utf-8"))
            repairs = [
                event.payload
                for event in runtime.events("run-empty-repair", limit=200)
                if event.event_type == "repair_attempted"
            ]
            self.assertEqual(["patch"], [item["stage"] for item in repairs])
            self.assertTrue(any("REPAIR 1/1" in line for line in output))
            self.assertEqual("completed", runtime.get_run("run-empty-repair").status)
            git.close()

    def test_transient_worker_failure_is_repaired_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            worker = ScriptedWorkerAgent([
                RuntimeError("transient worker crash"),
                _worker_response({"recovered.txt": "ok"}),
            ])
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-worker-repair",
            )
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("recover a transient worker failure")

            self.assertEqual(2, worker.call_count)
            self.assertEqual("ok\n", (root / "recovered.txt").read_text(encoding="utf-8"))
            events = runtime.events("run-worker-repair", limit=200)
            repairs = [
                event.payload
                for event in events
                if event.event_type == "repair_attempted"
            ]
            self.assertEqual(["worker"], [item["stage"] for item in repairs])
            versions = [
                event for event in events
                if event.event_type == "patch_version_captured"
            ]
            self.assertEqual(1, len(versions))
            self.assertEqual("completed", runtime.get_run("run-worker-repair").status)
            git.close()

    def test_no_progress_repair_exhausts_budget_without_applying_draft(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            before = self._commit_count(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-no-progress",
                steps=[{
                    "step_id": "step-1",
                    "title": "Pin dependencies",
                    "description": "Create requirements.txt with pinned dependencies.",
                    "type": "config",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "expected_output": "Runtime dependencies are exactly pinned.",
                    "context_hint": "",
                }],
            )
            unchanged = _worker_response({"draft.txt": "unchanged"})
            worker = ScriptedWorkerAgent([unchanged, unchanged])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            errors: list[str] = []
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task(
                "stop a no-progress repair",
                callbacks={"on_error": lambda step, reason: errors.append(reason)},
            )

            self.assertEqual(2, worker.call_count)
            self.assertEqual(1, len(errors))
            self.assertIn("no material patch change", errors[0].lower())
            self.assertIn("deterministic acceptance gates failed", errors[0].lower())
            self.assertIn("budget exhausted", errors[0].lower())
            self.assertEqual(before, self._commit_count(root))
            self.assertFalse((root / "draft.txt").exists())
            self.assertEqual({}, planner.review_counts)
            self.assertEqual("blocked", runtime.get_run("run-no-progress").status)
            events = runtime.events("run-no-progress", limit=200)
            self.assertEqual(
                1,
                sum(event.event_type == "repair_attempted" for event in events),
            )
            self.assertEqual(
                1,
                sum(event.event_type == "repair_budget_exhausted" for event in events),
            )
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
            palace = PalaceStore(cfg.runtime.state_db)

            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-ok"),
                {"fake-worker": FakeWorkerAgent(filename="ok.txt", content="ok")},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=palace,
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
            outcomes = palace.recent(room="run-ok", closet="steps")
            self.assertEqual(1, len(outcomes))
            self.assertNotIn("Patch:\n", outcomes[0].content)
            self.assertIn(step.patch_artifact_id, outcomes[0].content)
            self.assertIn(step.commit_sha, outcomes[0].content)
            git.close()

    def test_markdown_memory_failure_does_not_undo_committed_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-memory-failure"),
                {"fake-worker": FakeWorkerAgent(filename="durable.txt", content="ok")},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            with patch.object(
                memory,
                "append_step",
                side_effect=OSError("disk full"),
            ):
                orchestrator.run_task("commit despite supplemental memory failure")

            self.assertEqual("ok\n", (root / "durable.txt").read_text(encoding="utf-8"))
            self.assertEqual("completed", runtime.get_run("run-memory-failure").status)
            failures = [
                event for event in runtime.events("run-memory-failure")
                if event.event_type == "memory_write_failed"
            ]
            self.assertEqual(1, len(failures))
            self.assertEqual("append_step", failures[0].payload["operation"])
            git.close()

    def test_observer_callback_exceptions_do_not_fail_successful_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-callbacks"),
                {"fake-worker": ProgressWorkerAgent(filename="ok.txt", content="ok")},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            observed: set[str] = set()
            output_context: list[tuple[str, str, str]] = []

            def exploding(name: str):
                def callback(*args, **kwargs):
                    observed.add(name)
                    if name == "on_output" and args:
                        line = args[0]
                        output_context.append((
                            str(line),
                            getattr(line, "step_id", ""),
                            getattr(line, "worker_name", ""),
                        ))
                    raise RuntimeError(f"observer failed: {name}")
                return callback

            callback_names = {
                "on_status",
                "on_plan",
                "on_step_start",
                "on_worker_assigned",
                "on_output",
                "on_step_result",
                "on_review",
                "on_commit",
                "on_step_complete",
                "on_task_complete",
            }
            callbacks = {name: exploding(name) for name in callback_names}

            orchestrator.run_task("complete despite observer errors", callbacks=callbacks)

            self.assertEqual(callback_names, observed)
            self.assertIn(
                ("worker progress", "step-1", "fake-worker"),
                output_context,
            )
            self.assertEqual("ok\n", (root / "ok.txt").read_text(encoding="utf-8"))
            self.assertEqual("completed", runtime.get_run("run-callbacks").status)
            git.close()

    def test_on_error_callback_exception_does_not_hide_blocked_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="rejected", task_id="run-error-callback"),
                {"fake-worker": FakeWorkerAgent()},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            called = []

            def on_error(*args, **kwargs):
                called.append(True)
                raise RuntimeError("observer failed")

            orchestrator.run_task(
                "block despite observer error",
                callbacks={"on_error": on_error},
            )

            self.assertEqual([True], called)
            self.assertEqual("blocked", runtime.get_run("run-error-callback").status)
            WorktreeManager(root).cleanup_run("run-error-callback")
            git.close()

    def test_apply_failure_does_not_record_approved_outcome_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 0
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            runtime = RuntimeStore(cfg.runtime.state_db)
            palace = PalaceStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                FakePlanReviewAgent(verdict="approved", task_id="run-apply-failure"),
                {"fake-worker": FakeWorkerAgent(filename="ok.txt", content="ok")},
                memory,
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=palace,
                policy=ExecutionPolicy(),
            )

            with patch.object(
                WorktreeManager,
                "apply_patch",
                side_effect=RuntimeError("forced apply failure"),
            ):
                orchestrator.run_task("fail while applying approved patch")

            memory_text = memory.read()
            self.assertNotIn("**Status:** approved", memory_text)
            self.assertIn("**Status:** rejected", memory_text)
            self.assertEqual(
                [],
                palace.recent(room="run-apply-failure", closet="steps"),
            )
            self.assertEqual("blocked", runtime.get_run("run-apply-failure").status)
            self.assertFalse((root / "ok.txt").exists())
            WorktreeManager(root).cleanup_run("run-apply-failure")
            git.close()

    def test_apply_conflict_refreshes_base_and_repairs_instead_of_discarding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-integration-apply-repair",
            )
            worker = ScriptedWorkerAgent([
                _worker_response({"repair.txt": "first"}),
                _worker_response({"repair.txt": "reconciled"}),
            ])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            real_apply = WorktreeManager.apply_patch
            apply_calls = 0

            def fail_first_apply(manager, patch_text):
                nonlocal apply_calls
                apply_calls += 1
                if apply_calls == 1:
                    raise RuntimeError("main advanced after review")
                return real_apply(manager, patch_text)

            with patch.object(
                WorktreeManager,
                "apply_patch",
                new=fail_first_apply,
            ):
                orchestrator.run_task("repair an integration apply conflict")

            self.assertEqual(2, worker.call_count)
            self.assertEqual(2, planner.review_counts.get("step-1"))
            self.assertEqual(
                "reconciled\n",
                (root / "repair.txt").read_text(encoding="utf-8"),
            )
            record = runtime.get_step(
                "run-integration-apply-repair", "step-1"
            )
            self.assertEqual("committed", record.status)
            self.assertEqual("resolved", record.metadata["repair_state"])
            self.assertEqual([], record.metadata["retained_worktrees"])
            repairs = [
                event
                for event in runtime.events(
                    "run-integration-apply-repair", limit=300
                )
                if event.event_type == "repair_attempted"
            ]
            self.assertEqual(1, len(repairs))
            self.assertEqual("integration_apply", repairs[0].payload["stage"])
            self.assertTrue(any(
                event.event_type == "repair_resolved"
                for event in runtime.events(
                    "run-integration-apply-repair", limit=300
                )
            ))
            git.close()

    def test_transient_commit_failure_revalidates_same_patch_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-integration-commit-repair",
            )
            worker = ScriptedWorkerAgent([
                _worker_response({"commit-retry.txt": "durable"}),
            ])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            real_commit = git.commit_step
            failed_step_commit = False

            def fail_first_step_commit(step_id, title, paths=None, **kwargs):
                nonlocal failed_step_commit
                if step_id == "step-1" and not failed_step_commit:
                    failed_step_commit = True
                    return None
                return real_commit(step_id, title, paths, **kwargs)

            with patch.object(
                git,
                "commit_step",
                side_effect=fail_first_step_commit,
            ):
                orchestrator.run_task("retry a transient integration commit")

            self.assertEqual(1, worker.call_count)
            self.assertEqual(2, planner.review_counts.get("step-1"))
            self.assertEqual(
                "durable\n",
                (root / "commit-retry.txt").read_text(encoding="utf-8"),
            )
            record = runtime.get_step(
                "run-integration-commit-repair", "step-1"
            )
            self.assertEqual("committed", record.status)
            self.assertEqual("resolved", record.metadata["repair_state"])
            repairs = [
                event
                for event in runtime.events(
                    "run-integration-commit-repair", limit=300
                )
                if event.event_type == "repair_attempted"
            ]
            self.assertEqual(1, len(repairs))
            self.assertEqual("integration_commit", repairs[0].payload["stage"])
            git.close()

    def test_commit_failure_rolls_approved_patch_back_out_of_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            memory = MemoryManager(str(root / "GENESIS_MEMORY.md"))
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                FakePlanReviewAgent(
                    verdict="approved",
                    task_id="run-commit-failure",
                    verdict_sequences_by_step={
                        "step-1": ["needs_revision", "approved"]
                    },
                ),
                {
                    "fake-worker": FakeWorkerAgent(
                        filename="not-committed.txt",
                        content="first",
                        repair_files_by_step={
                            "step-1": ("not-committed.txt", "fixed")
                        },
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

            with patch.object(git, "commit_step", return_value=None):
                orchestrator.run_task("roll back a failed integration commit")

            self.assertFalse((root / "not-committed.txt").exists())
            self.assertEqual(
                "",
                self._git_output(root, "status", "--short", "--", "not-committed.txt"),
            )
            self.assertEqual("blocked", runtime.get_run("run-commit-failure").status)
            self.assertNotIn("**Status:** approved", memory.read())
            step = runtime.get_step("run-commit-failure", "step-1")
            self.assertEqual("integration_failed", step.metadata["repair_state"])
            event_types = [
                event.event_type
                for event in runtime.events("run-commit-failure", limit=250)
            ]
            self.assertIn("repair_integration_failed", event_types)
            self.assertNotIn("repair_resolved", event_types)
            WorktreeManager(root).cleanup_run("run-commit-failure")
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

    def test_verifier_mutation_is_re_reviewed_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.dialogue.enabled = False
            cfg.verification.commands = [
                "python -c \"from pathlib import Path; "
                "Path('result.txt').write_text('fixed\\n', encoding='utf-8')\""
            ]
            worker = ScriptedWorkerAgent([
                _worker_response({"result.txt": "bad"}),
            ])
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-verifier-mutation",
            )
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("accept only the bytes verification observed")

            self.assertEqual(1, worker.call_count)
            self.assertEqual(2, planner.review_counts.get("step-1"))
            self.assertEqual("fixed\n", (root / "result.txt").read_text(encoding="utf-8"))
            self.assertEqual("completed", runtime.get_run("run-verifier-mutation").status)
            events = runtime.events("run-verifier-mutation", limit=250)
            repairs = [
                event.payload
                for event in events
                if event.event_type == "repair_attempted"
            ]
            self.assertEqual(["verification_mutation"], [item["stage"] for item in repairs])
            self.assertTrue(any(
                event.event_type == "review_superseded" for event in events
            ))
            self.assertTrue(any(
                event.event_type == "repair_resolved" for event in events
            ))
            git.close()

    def test_dialogue_repairs_obey_zero_shared_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 0
            cfg.dialogue.enabled = True
            cfg.dialogue.max_turns = 2
            cfg.dialogue.fast_path = True
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-dialogue-budget",
                steps=[{
                    "step_id": "step-1",
                    "title": "Pin dependencies",
                    "description": "Create requirements.txt with pinned dependencies.",
                    "type": "config",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "expected_output": "Runtime dependencies are exactly pinned.",
                    "context_hint": "",
                }],
            )
            worker = ScriptedWorkerAgent([
                _worker_response({"draft.txt": "first"}),
                _worker_response({"requirements.txt": "requests==2.32.3"}),
            ])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("do not bypass the shared repair budget")

            self.assertEqual(1, worker.call_count)
            self.assertEqual("blocked", runtime.get_run("run-dialogue-budget").status)
            self.assertFalse((root / "requirements.txt").exists())
            repairs = [
                event
                for event in runtime.events("run-dialogue-budget", limit=200)
                if event.event_type == "repair_attempted"
            ]
            self.assertEqual([], repairs)
            git.close()

    def test_dialogue_no_progress_cannot_be_reported_as_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.dialogue.enabled = True
            cfg.dialogue.fast_path = False
            cfg.dialogue.max_turns = 2
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-dialogue-no-progress",
                dialogue_actions=[("revise", "Make a material correction.")],
            )
            unchanged = _worker_response({"candidate.txt": "unchanged"})
            worker = ScriptedWorkerAgent([unchanged, unchanged])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("reject a no-op dialogue repair")

            self.assertEqual(2, worker.call_count)
            self.assertEqual(1, planner.dialogue_calls)
            self.assertEqual({}, planner.review_counts)
            self.assertFalse((root / "candidate.txt").exists())
            self.assertEqual(
                "blocked",
                runtime.get_run("run-dialogue-no-progress").status,
            )
            events = runtime.events("run-dialogue-no-progress", limit=250)
            self.assertFalse(any(
                event.event_type == "repair_resolved" for event in events
            ))
            step = runtime.get_step("run-dialogue-no-progress", "step-1")
            self.assertEqual("repair_exhausted", step.metadata["repair_state"])
            WorktreeManager(root).cleanup_run("run-dialogue-no-progress")
            git.close()

    def test_worker_infrastructure_failure_is_not_retried_as_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 2
            cfg.dialogue.enabled = False
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-worker-infrastructure",
            )
            worker = ScriptedWorkerAgent([
                RuntimeError("No space left on device"),
                _worker_response({"unexpected.txt": "must not run"}),
            ])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("hard-block infrastructure failures")

            self.assertEqual(1, worker.call_count)
            self.assertFalse((root / "unexpected.txt").exists())
            step = runtime.get_step("run-worker-infrastructure", "step-1")
            self.assertEqual("hard_failure", step.metadata["block_kind"])
            self.assertEqual("blocked", step.status)
            self.assertFalse(any(
                event.event_type == "repair_attempted"
                for event in runtime.events("run-worker-infrastructure", limit=200)
            ))
            WorktreeManager(root).cleanup_run("run-worker-infrastructure")
            git.close()

    def test_interrupted_ready_repair_resumes_without_free_worker_turn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.dialogue.enabled = False
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-resume-ready-repair",
            )
            worker = ScriptedWorkerAgent([])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            plan = Plan(
                task_id="run-resume-ready-repair",
                task_summary="resume retained repair",
                estimated_steps=1,
                steps=planner.steps,
            )
            orchestrator._save_plan(
                "resume an interrupted repair",
                plan,
                status="running",
            )
            worktrees = WorktreeManager(root)
            retained = worktrees.create(plan.task_id, "step-1")
            (retained / "retained.txt").write_text("fixed\n", encoding="utf-8")
            retained_patch = worktrees.capture_patch(retained, plan.steps[0])
            runtime.upsert_step(
                plan.task_id,
                "step-1",
                title="Write file",
                status="repairing",
                worker="scripted-worker",
                worktree_path=str(retained),
                metadata={
                    "repair_attempts": 1,
                    "repair_budget": 1,
                    "repair_state": "candidate_ready",
                    "repair_stage": "acceptance",
                    "repair_id": "repair-ready-1",
                    "repair_prior_patch_sha": "superseded-patch",
                    "last_repair_reason": "required artifact was missing",
                    "current_patch_sha": retained_patch.patch_sha,
                    "current_patch_version": 1,
                },
            )

            orchestrator.resume_task(plan.task_id)

            self.assertEqual(0, worker.call_count)
            self.assertEqual(
                "fixed\n",
                (root / "retained.txt").read_text(encoding="utf-8"),
            )
            step = runtime.get_step(plan.task_id, "step-1")
            self.assertEqual("committed", step.status)
            self.assertEqual("resolved", step.metadata["repair_state"])
            self.assertTrue(any(
                event.event_type == "repair_resolved"
                for event in runtime.events(plan.task_id, limit=250)
            ))
            git.close()

    def test_new_patch_identity_clears_stale_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            run_id = "run-clear-stale-verification"
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.dialogue.enabled = False
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(verdict="approved", task_id=run_id)
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": ScriptedWorkerAgent([])},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            plan = Plan(
                task_id=run_id,
                task_summary="clear stale verification",
                estimated_steps=1,
                steps=planner.steps,
            )
            orchestrator._save_plan("clear stale verification", plan, status="running")
            worktrees = WorktreeManager(root)
            retained = worktrees.create(run_id, "step-1")
            candidate = retained / "candidate.txt"
            candidate.write_text("first\n", encoding="utf-8")
            first_patch = worktrees.capture_patch(retained, plan.steps[0])
            runtime.upsert_step(
                run_id,
                "step-1",
                status="verifying",
                worktree_path=str(retained),
                verification={
                    "passed": True,
                    "verified_patch_sha": first_patch.patch_sha,
                },
                metadata={
                    "current_patch_sha": first_patch.patch_sha,
                    "reviewed_patch_sha": first_patch.patch_sha,
                    "verified_patch_sha": first_patch.patch_sha,
                },
            )

            candidate.write_text("replacement\n", encoding="utf-8")
            replacement = worktrees.capture_patch(retained, plan.steps[0])
            orchestrator._record_patch_version(
                run_id,
                plan.steps[0],
                replacement,
                2,
            )

            record = runtime.get_step(run_id, "step-1")
            self.assertEqual({}, record.verification_json)
            self.assertEqual("", record.metadata["verified_patch_sha"])
            self.assertEqual(replacement.patch_sha, record.metadata["current_patch_sha"])
            self.assertEqual({}, record.review_json)
            worktrees.cleanup_run(run_id)
            git.close()

    def test_reconciliation_rejects_stale_verification_patch_sha(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            run_id = "run-reject-stale-verification"
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.dialogue.enabled = False
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(verdict="approved", task_id=run_id)
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": ScriptedWorkerAgent([])},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            plan = Plan(
                task_id=run_id,
                task_summary="reject stale verification",
                estimated_steps=1,
                steps=planner.steps,
            )
            orchestrator._save_plan("reject stale verification", plan, status="running")
            worktrees = WorktreeManager(root)
            retained = worktrees.create(run_id, "step-1")
            (retained / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            candidate = worktrees.capture_patch(retained, plan.steps[0])
            runtime.upsert_step(
                run_id,
                "step-1",
                title="Write file",
                status="verifying",
                worker="scripted-worker",
                worktree_path=str(retained),
                review={
                    "step_id": "step-1",
                    "verdict": "approved",
                    "quality_score": 9,
                    "feedback": "",
                    "memory_note": "The candidate was reviewed.",
                    "should_retry": False,
                    "suggested_revision": "",
                },
                verification={
                    "passed": True,
                    "skipped": False,
                    "reason": "stale result",
                    "failure_kind": "",
                    "repairable": True,
                    "commands": [],
                    "verified_patch_sha": "older-patch",
                },
                metadata={
                    "current_patch_sha": candidate.patch_sha,
                    "reviewed_patch_sha": candidate.patch_sha,
                    "verified_patch_sha": "older-patch",
                },
            )
            worktrees.apply_patch(candidate.patch_text)

            orchestrator._reconcile_integrated_steps(
                plan,
                worktrees,
                lambda *_args, **_kwargs: None,
            )

            record = runtime.get_step(run_id, "step-1")
            self.assertEqual("verifying", record.status)
            self.assertEqual("", record.commit_sha)
            self.assertFalse(any(
                event.event_type == "integration_reconciled"
                for event in runtime.events(run_id, limit=100)
            ))
            worktrees.cleanup_run(run_id)
            git.close()

    def test_commit_rollback_crash_resumes_without_free_worker_turn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            run_id = "run-rollback-pending"
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.dialogue.enabled = False
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(verdict="approved", task_id=run_id)
            worker = ScriptedWorkerAgent([])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            plan = Plan(
                task_id=run_id,
                task_summary="resume a journaled rollback",
                estimated_steps=1,
                steps=planner.steps,
            )
            orchestrator._save_plan(
                "resume after rollback completed before state update",
                plan,
                status="running",
            )
            worktrees = WorktreeManager(root)
            retained = worktrees.create(run_id, "step-1")
            (retained / "rollback-safe.txt").write_text(
                "reviewed\n", encoding="utf-8"
            )
            candidate = worktrees.capture_patch(retained, plan.steps[0])
            review = {
                "step_id": "step-1",
                "verdict": "approved",
                "quality_score": 9,
                "feedback": "",
                "memory_note": "The retained candidate was reviewed.",
                "should_retry": False,
                "suggested_revision": "",
            }
            verification = {
                "passed": True,
                "skipped": True,
                "reason": "no verification commands configured",
                "failure_kind": "",
                "repairable": True,
                "commands": [],
                "verified_patch_sha": candidate.patch_sha,
            }
            runtime.upsert_step(
                run_id,
                "step-1",
                title="Write file",
                status="repairing",
                worker="scripted-worker",
                worktree_path=str(retained),
                review=review,
                verification=verification,
                metadata={
                    "repair_attempts": 1,
                    "repair_budget": 1,
                    "repair_state": "integration_rollback_pending",
                    "repair_stage": "integration_commit",
                    "repair_id": "rollback-pending-1",
                    "repair_prior_patch_sha": candidate.patch_sha,
                    "current_patch_sha": candidate.patch_sha,
                    "current_patch_version": 1,
                    "reviewed_patch_sha": candidate.patch_sha,
                    "review_patch_version": 1,
                    "verified_patch_sha": candidate.patch_sha,
                },
            )

            orchestrator.resume_task(run_id)

            self.assertEqual(0, worker.call_count)
            self.assertEqual(
                "reviewed\n",
                (root / "rollback-safe.txt").read_text(encoding="utf-8"),
            )
            record = runtime.get_step(run_id, "step-1")
            self.assertEqual("committed", record.status)
            self.assertEqual("resolved", record.metadata["repair_state"])
            git.close()

    def test_resume_finishes_durably_pending_committed_worktree_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            run_id = "run-cleanup-pending"
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.dialogue.enabled = False
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(verdict="approved", task_id=run_id)
            worker = ScriptedWorkerAgent([])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            plan = Plan(
                task_id=run_id,
                task_summary="finish committed cleanup",
                estimated_steps=1,
                steps=planner.steps,
            )
            orchestrator._save_plan(
                "resume after commit before worktree deletion",
                plan,
                status="running",
            )
            worktrees = WorktreeManager(root)
            pending = worktrees.create(run_id, "step-1")
            head = self._git_output(root, "rev-parse", "--short", "HEAD").strip()
            runtime.upsert_step(
                run_id,
                "step-1",
                title="Write file",
                status="committed",
                worktree_path=str(pending),
                commit_sha=head,
                metadata={
                    "cleanup_pending_worktrees": [str(pending)],
                    "retained_worktrees": [],
                },
            )

            orchestrator.resume_task(run_id)

            self.assertEqual(0, worker.call_count)
            self.assertFalse(pending.exists())
            record = runtime.get_step(run_id, "step-1")
            self.assertEqual("", record.worktree_path)
            self.assertEqual([], record.metadata["cleanup_pending_worktrees"])
            self.assertTrue(any(
                event.event_type == "worktree_cleanup_completed"
                for event in runtime.events(run_id, limit=200)
            ))
            git.close()

    def test_pending_pre_rollback_crash_restores_main_before_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            run_id = "run-pre-rollback-crash"
            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(verdict="approved", task_id=run_id)
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": ScriptedWorkerAgent([])},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )
            plan = Plan(
                task_id=run_id,
                task_summary="recover before rollback",
                estimated_steps=1,
                steps=planner.steps,
            )
            orchestrator._save_plan("recover rollback intent", plan, status="running")
            worktrees = WorktreeManager(root)
            retained = worktrees.create(run_id, "step-1")
            (retained / "pending.txt").write_text("candidate\n", encoding="utf-8")
            candidate = worktrees.capture_patch(retained, plan.steps[0])
            runtime.upsert_step(
                run_id,
                "step-1",
                title="Write file",
                status="repairing",
                worker="scripted-worker",
                worktree_path=str(retained),
                metadata={
                    "repair_attempts": 1,
                    "repair_budget": 1,
                    "repair_state": "integration_rollback_pending",
                    "repair_stage": "integration_commit",
                    "current_patch_sha": candidate.patch_sha,
                },
            )
            worktrees.apply_patch(candidate.patch_text)
            self.assertTrue((root / "pending.txt").exists())

            safe = orchestrator._complete_pending_integration_rollbacks(
                plan,
                worktrees,
                lambda *args, **kwargs: None,
            )

            self.assertTrue(safe)
            self.assertFalse((root / "pending.txt").exists())
            self.assertTrue(git.paths_match_head(candidate.changed_files))
            record = runtime.get_step(run_id, "step-1")
            self.assertEqual("repairing", record.status)
            self.assertEqual("validating", record.metadata["repair_state"])
            WorktreeManager(root).cleanup_run(run_id)
            git.close()

    def test_resume_reconciles_patch_applied_before_runtime_commit(self) -> None:
        for recovery_state in ("dirty", "committed", "reverted_dirty"):
            with self.subTest(recovery_state=recovery_state):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    self._init_repo(root)

                    run_id = f"run-reconcile-{recovery_state}"
                    cfg = GenesisConfig()
                    cfg.runtime.state_db = str(
                        root / ".genesis" / "state" / "state.db"
                    )
                    cfg.runtime.retry_budget = 1
                    cfg.dialogue.enabled = False
                    cfg.verification.commands = []
                    planner = FakePlanReviewAgent(
                        verdict="approved",
                        task_id=run_id,
                    )
                    worker = ScriptedWorkerAgent([])
                    runtime = RuntimeStore(cfg.runtime.state_db)
                    git = GitManager(str(root), cfg.git)
                    orchestrator = Orchestrator(
                        planner,
                        {"scripted-worker": worker},
                        MemoryManager(str(root / "GENESIS_MEMORY.md")),
                        git,
                        cfg,
                        str(root),
                        runtime=runtime,
                        palace=PalaceStore(cfg.runtime.state_db),
                        policy=ExecutionPolicy(),
                    )
                    plan = Plan(
                        task_id=run_id,
                        task_summary="reconcile integrated repair",
                        estimated_steps=1,
                        steps=planner.steps,
                    )
                    orchestrator._save_plan(
                        "reconcile a Git/runtime crash gap",
                        plan,
                        status="running",
                    )
                    worktrees = WorktreeManager(root)
                    retained = worktrees.create(run_id, "step-1")
                    older_draft = worktrees.create(run_id, "older-draft")
                    (retained / "reconciled.txt").write_text(
                        "fixed\n", encoding="utf-8"
                    )
                    retained_patch = worktrees.capture_patch(
                        retained, plan.steps[0]
                    )
                    review = {
                        "step_id": "step-1",
                        "verdict": "approved",
                        "quality_score": 9,
                        "feedback": "",
                        "memory_note": "The repaired file was reviewed.",
                        "should_retry": False,
                        "suggested_revision": "",
                    }
                    verification = {
                        "passed": True,
                        "skipped": True,
                        "reason": "no verification commands configured",
                        "failure_kind": "",
                        "repairable": True,
                        "commands": [],
                        "verified_patch_sha": retained_patch.patch_sha,
                    }
                    runtime.upsert_step(
                        run_id,
                        "step-1",
                        title="Write file",
                        status="verifying",
                        worker="scripted-worker",
                        worktree_path=str(retained),
                        review=review,
                        verification=verification,
                        metadata={
                            "reviewer": "fake-orchestrator",
                            "repair_attempts": 1,
                            "repair_budget": 1,
                            "repair_state": "candidate_ready",
                            "repair_stage": "review",
                            "repair_id": "repair-integration-gap",
                            "current_patch_sha": retained_patch.patch_sha,
                            "reviewed_patch_sha": retained_patch.patch_sha,
                            "verified_patch_sha": retained_patch.patch_sha,
                            "current_patch_version": 2,
                            "review_patch_version": 2,
                            "retained_worktrees": [str(older_draft)],
                        },
                    )
                    worktrees.apply_patch(retained_patch.patch_text)
                    committed_before_resume = ""
                    if recovery_state in {"committed", "reverted_dirty"}:
                        committed_before_resume = git.commit_step(
                            "step-1",
                            "Write file",
                            paths=retained_patch.changed_files,
                            patch_sha=retained_patch.patch_sha,
                            run_id=run_id,
                        ) or ""
                    if recovery_state == "reverted_dirty":
                        subprocess.run(
                            ["git", "revert", "--no-edit", committed_before_resume],
                            cwd=root,
                            check=True,
                            capture_output=True,
                        )
                        worktrees.apply_patch(retained_patch.patch_text)

                    orchestrator.resume_task(run_id)

                    self.assertEqual(0, worker.call_count)
                    self.assertEqual(
                        "fixed\n",
                        (root / "reconciled.txt").read_text(encoding="utf-8"),
                    )
                    step = runtime.get_step(run_id, "step-1")
                    self.assertEqual("committed", step.status)
                    self.assertEqual("resolved", step.metadata["repair_state"])
                    self.assertTrue(step.metadata["reconciled_integration"])
                    self.assertFalse(older_draft.exists())
                    self.assertEqual("", step.worktree_path)
                    self.assertEqual(
                        [], step.metadata["cleanup_pending_worktrees"]
                    )
                    if recovery_state == "committed":
                        self.assertEqual(committed_before_resume, step.commit_sha)
                    elif recovery_state == "reverted_dirty":
                        self.assertNotEqual(committed_before_resume, step.commit_sha)
                        self.assertEqual(
                            0,
                            subprocess.run(
                                [
                                    "git",
                                    "merge-base",
                                    "--is-ancestor",
                                    step.commit_sha,
                                    "HEAD",
                                ],
                                cwd=root,
                                check=False,
                                capture_output=True,
                            ).returncode,
                        )
                        self.assertEqual(
                            "",
                            self._git_output(
                                root,
                                "status",
                                "--short",
                                "--",
                                "reconciled.txt",
                            ).strip(),
                        )
                    event_types = [
                        event.event_type
                        for event in runtime.events(run_id, limit=300)
                    ]
                    self.assertIn("integration_reconciled", event_types)
                    self.assertIn("repair_resolved", event_types)
                    self.assertEqual("completed", runtime.get_run(run_id).status)
                    git.close()

    def test_failed_repair_committed_as_advisory_is_not_called_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.dialogue.enabled = False
            cfg.verification.commands = [
                'python -c "raise SystemExit(1)"'
            ]
            cfg.verification.require_for_commit = False
            planner = FakePlanReviewAgent(
                verdict="approved",
                task_id="run-repair-advisory",
            )
            worker = ScriptedWorkerAgent([
                _worker_response({"advisory.txt": "first"}),
                _worker_response({"advisory.txt": "second"}),
            ])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("retain honest advisory repair state")

            self.assertEqual("second\n", (root / "advisory.txt").read_text(encoding="utf-8"))
            step = runtime.get_step("run-repair-advisory", "step-1")
            self.assertEqual("committed", step.status)
            self.assertFalse(step.verification_json["passed"])
            self.assertEqual("accepted_advisory", step.metadata["repair_state"])
            event_types = [
                event.event_type
                for event in runtime.events("run-repair-advisory", limit=250)
            ]
            self.assertIn("repair_advisory_accepted", event_types)
            self.assertNotIn("repair_resolved", event_types)
            git.close()

    def test_reviewer_retry_flags_must_be_consistent(self) -> None:
        cases = (
            ("needs_revision", False),
            ("rejected", True),
        )
        for verdict, retry_flag in cases:
            with self.subTest(verdict=verdict, retry_flag=retry_flag):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    self._init_repo(root)
                    cfg = GenesisConfig()
                    cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
                    cfg.runtime.retry_budget = 1
                    cfg.dialogue.enabled = False
                    cfg.verification.commands = []
                    planner = FakePlanReviewAgent(
                        verdict=verdict,
                        retry_override=retry_flag,
                        task_id=f"run-review-flags-{verdict}",
                    )
                    worker = ScriptedWorkerAgent([
                        _worker_response({"candidate.txt": "candidate"}),
                        _worker_response({"candidate.txt": "unexpected repair"}),
                    ])
                    runtime = RuntimeStore(cfg.runtime.state_db)
                    git = GitManager(str(root), cfg.git)
                    orchestrator = Orchestrator(
                        planner,
                        {"scripted-worker": worker},
                        MemoryManager(str(root / "GENESIS_MEMORY.md")),
                        git,
                        cfg,
                        str(root),
                        runtime=runtime,
                        palace=PalaceStore(cfg.runtime.state_db),
                        policy=ExecutionPolicy(),
                    )

                    orchestrator.run_task("honor the review retry contract")

                    self.assertEqual(1, worker.call_count)
                    self.assertEqual(
                        "blocked",
                        runtime.get_run(f"run-review-flags-{verdict}").status,
                    )
                    repairs = [
                        event
                        for event in runtime.events(
                            f"run-review-flags-{verdict}", limit=200
                        )
                        if event.event_type == "repair_attempted"
                    ]
                    self.assertEqual([], repairs)
                    git.close()

    def test_reviewer_step_mismatch_retries_same_patch_then_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.retry_budget = 1
            cfg.dialogue.enabled = False
            cfg.verification.commands = []
            planner = FakePlanReviewAgent(
                verdict="approved",
                review_step_id="different-step",
                task_id="run-review-mismatch",
            )
            worker = ScriptedWorkerAgent([
                _worker_response({"candidate.txt": "candidate"}),
            ])
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            orchestrator = Orchestrator(
                planner,
                {"scripted-worker": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("reject mismatched reviewer identity")
            git.close()

            self.assertEqual(1, worker.call_count)
            self.assertEqual(2, planner.review_counts.get("step-1"))
            self.assertFalse((root / "candidate.txt").exists())
            self.assertNotIn(
                "step-1: Write file",
                self._git_output(root, "log", "--format=%s"),
            )
            self.assertEqual("blocked", runtime.get_run("run-review-mismatch").status)
            events = runtime.events("run-review-mismatch", limit=200)
            self.assertEqual(
                1,
                sum(event.event_type == "review_retried" for event in events),
            )
            step = runtime.get_step("run-review-mismatch", "step-1")
            self.assertIn("expected 'step-1'", step.metadata["blocked_reason"])

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

    def test_failed_branch_does_not_stop_an_independent_branch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)

            cfg = GenesisConfig()
            cfg.runtime.state_db = str(root / ".genesis" / "state" / "state.db")
            cfg.runtime.max_parallel_workers = 2
            cfg.verification.commands = []
            cfg.dialogue.enabled = False
            runtime = RuntimeStore(cfg.runtime.state_db)
            git = GitManager(str(root), cfg.git)
            steps = [
                {
                    "step_id": "step-1",
                    "title": "Rejected branch",
                    "description": "Create rejected.txt.",
                    "type": "code",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "file_scope": ["rejected.txt"],
                    "expected_output": "rejected.txt exists.",
                    "context_hint": "",
                },
                {
                    "step_id": "step-2",
                    "title": "Independent branch",
                    "description": "Create accepted.txt.",
                    "type": "code",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "file_scope": ["accepted.txt"],
                    "expected_output": "accepted.txt exists.",
                    "context_hint": "",
                },
            ]
            planner = FakePlanReviewAgent(
                task_id="run-partial",
                steps=steps,
                verdicts_by_step={"step-1": "rejected", "step-2": "approved"},
            )
            worker = FakeWorkerAgent(files_by_step={
                "step-1": ("rejected.txt", "no"),
                "step-2": ("accepted.txt", "yes"),
            })
            orchestrator = Orchestrator(
                planner,
                {"worker-a": worker, "worker-b": worker},
                MemoryManager(str(root / "GENESIS_MEMORY.md")),
                git,
                cfg,
                str(root),
                runtime=runtime,
                palace=PalaceStore(cfg.runtime.state_db),
                policy=ExecutionPolicy(),
            )

            orchestrator.run_task("run independent branches")

            self.assertFalse((root / "rejected.txt").exists())
            self.assertEqual("yes\n", (root / "accepted.txt").read_text(encoding="utf-8"))
            self.assertEqual("blocked", runtime.get_step("run-partial", "step-1").status)
            self.assertEqual("committed", runtime.get_step("run-partial", "step-2").status)
            self.assertEqual("blocked", runtime.get_run("run-partial").status)
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
