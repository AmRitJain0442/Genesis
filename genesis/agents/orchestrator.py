from __future__ import annotations
import json
import uuid
import logging
from typing import Callable, TYPE_CHECKING

from genesis.schemas.plan import Plan, Step
from genesis.schemas.review import Review
from genesis.agents.worker import Worker, WorkerResult
from genesis.memory import MemoryManager
from genesis.git_ops import GitManager
from genesis.config import GenesisConfig

if TYPE_CHECKING:
    from genesis.agents.base import BaseAgent


def _make_worker(agent: BaseAgent, memory_summary: str, work_dir: str,
                 output_callback=None):
    """Return the right worker type for the given agent."""
    try:
        from genesis.agents.codex_cli import CodexCLIAgent
        from genesis.agents.codex_worker import CodexWorker
        if isinstance(agent, CodexCLIAgent):
            return CodexWorker(agent, memory_summary, work_dir,
                               output_callback=output_callback)
    except ImportError:
        pass
    return Worker(agent, memory_summary, work_dir, output_callback=output_callback)

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are Genesis Orchestrator — the director of an AI software development firm.

Your two responsibilities are PLANNING and REVIEWING.

Available worker agents:
- codex-worker: Autonomous code executor — writes files and runs shell commands directly.
  Use for ALL implementation, tests, config, and refactor tasks.
- claude-worker: Reserved — only specify if no codex worker is available.

━━━ PLANNING ━━━
When asked to plan a task, return ONLY a JSON object — no prose before or after:

{
  "task_id": "<8-char id>",
  "task_summary": "<one-sentence restatement of the task>",
  "estimated_steps": <int 1-12>,
  "steps": [
    {
      "step_id": "step-1",
      "title": "<≤60 chars>",
      "description": "<detailed instructions for the worker — be precise>",
      "type": "<code|docs|review|research|test|config|refactor>",
      "preferred_agent": "<codex-worker|any>",
      "depends_on": [],
      "expected_output": "<description of what success looks like>",
      "context_hint": "<optional: file paths, constraints, examples>"
    }
  ]
}

Planning rules:
- Break tasks into 3–10 concrete, atomic steps — each must produce a real artifact.
- Use depends_on to express ordering (step-2 depends on step-1 means step-1 runs first).
- Write description as if briefing a senior engineer: precise, complete, unambiguous.
- Study the memory context and do NOT re-plan work that is already done.

━━━ REVIEWING ━━━
When asked to review a step result, return ONLY a JSON object:

{
  "step_id": "<id>",
  "verdict": "<approved|needs_revision|rejected>",
  "quality_score": <1-10>,
  "feedback": "<specific actionable feedback if not approved, else empty string>",
  "memory_note": "<1-2 sentences: what was actually built/accomplished>",
  "should_retry": <true|false>,
  "suggested_revision": "<if should_retry: exact instructions to fix the problem>"
}

Review rules:
- Be strict: quality_score < 7 = needs_revision (unless fundamentally broken = rejected).
- Never approve code with syntax errors, incomplete stubs, or missing imports.
- memory_note must be factual (what was built), not aspirational (what was attempted).
- should_retry = true only when the fix is clear and a retry would plausibly succeed.
"""


class Orchestrator:
    def __init__(
        self,
        agent: BaseAgent,
        worker_agents: dict[str, BaseAgent],
        memory: MemoryManager,
        git: GitManager,
        config: GenesisConfig,
        work_dir: str = ".",
    ):
        self.agent = agent
        self.worker_agents = worker_agents
        self.memory = memory
        self.git = git
        self.config = config
        self.work_dir = work_dir

    # ── Public API ─────────────────────────────────────────────────────────

    def plan(self, task: str) -> Plan:
        mem = self.memory.get_summary(self.config.memory.max_context_chars)
        msg = (
            f"CURRENT MEMORY CONTEXT:\n{mem}\n\n---\n\n"
            f"TASK TO PLAN:\n{task}\n\n"
            f"Return the plan as JSON."
        )
        # Use chat_plan if available (ClaudeCodeCLIAgent with --json-schema)
        if hasattr(self.agent, "chat_plan"):
            raw = self.agent.chat_plan(_SYSTEM, [{"role": "user", "content": msg}])
        else:
            raw = self.agent.chat(_SYSTEM, [{"role": "user", "content": msg}])
        data = self._extract_json(raw)
        if not data.get("task_id"):
            data["task_id"] = str(uuid.uuid4())[:8]
        return Plan(**data)

    def review(self, step: Step, result: WorkerResult) -> Review:
        files_summary = (
            "Files written: " + ", ".join(result.files_written)
            if result.files_written
            else "No files written."
        )
        result_preview = result.result_text[:3000]
        msg = (
            f"Review this step result.\n\n"
            f"STEP:\n"
            f"  ID: {step.step_id}\n"
            f"  Title: {step.title}\n"
            f"  Type: {step.type}\n"
            f"  Expected Output: {step.expected_output}\n\n"
            f"WORKER RESULT:\n{files_summary}\n\n{result_preview}\n\n"
            f"Return your review as JSON."
        )
        # Use chat_review if available (ClaudeCodeCLIAgent with --json-schema)
        if hasattr(self.agent, "chat_review"):
            raw = self.agent.chat_review(_SYSTEM, [{"role": "user", "content": msg}])
        else:
            raw = self.agent.chat(_SYSTEM, [{"role": "user", "content": msg}])
        data = self._extract_json(raw)
        return Review(**data)

    def run_task(self, task: str, callbacks: dict[str, Callable] | None = None) -> None:
        cb = callbacks or {}
        output_callback = cb.get("on_output")

        def fire(name: str, *args, **kwargs) -> None:
            if fn := cb.get(name):
                fn(*args, **kwargs)

        # ── Plan ────────────────────────────────────────────────────────
        fire("on_status", "Planning task…")
        plan = self.plan(task)
        fire("on_plan", plan)

        if self.config.memory.auto_append_plan:
            self.memory.append_plan(plan)

        # ── Execute steps in dependency order ───────────────────────────
        steps = _topo_sort(plan.steps)
        completed = 0

        for i, step in enumerate(steps):
            fire("on_step_start", step, i, len(steps))

            worker_name, worker_agent = self._assign_worker(step)
            fire("on_worker_assigned", step, worker_name)

            mem_summary = self.memory.get_summary(self.config.memory.max_context_chars)
            worker = _make_worker(worker_agent, mem_summary, self.work_dir,
                                  output_callback=output_callback)

            result = worker.execute(step)

            if not result.success:
                fire("on_error", step, result.error)
                self.memory.append_step(
                    step.step_id, step.title, worker_name,
                    f"FAILED: {result.error}", "rejected",
                )
                continue

            fire("on_step_result", step, result, worker_name)

            # ── Review ──────────────────────────────────────────────────
            fire("on_status", f"Reviewing {step.step_id}…")
            review = self.review(step, result)
            fire("on_review", step, review)

            # ── Retry once if needed ────────────────────────────────────
            if review.verdict == "needs_revision" and review.should_retry:
                fire("on_status", f"Retrying {step.step_id} with feedback…")
                revised = step.model_copy(update={
                    "description": (
                        step.description
                        + f"\n\nREVISION REQUIRED: {review.suggested_revision}"
                    )
                })
                result = worker.execute(revised)
                review = self.review(revised, result)
                fire("on_review", step, review)

            # ── Memory + Git ────────────────────────────────────────────
            self.memory.append_step(
                step.step_id, step.title, worker_name,
                review.memory_note, review.verdict,
            )

            if self.config.git.auto_commit:
                sha = self.git.commit_step(step.step_id, step.title)
                if sha and self.config.git.auto_push:
                    self.git.push()
                fire("on_commit", step, sha)

            completed += 1
            fire("on_step_complete", step, review, completed, len(steps))

        # ── Task complete ────────────────────────────────────────────────
        self.memory.complete_task(plan.task_id)
        if self.config.git.auto_commit:
            sha = self.git.commit_step("task-complete", plan.task_summary[:60])
            if sha and self.config.git.auto_push:
                self.git.push()

        fire("on_task_complete", plan)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _assign_worker(self, step: Step) -> tuple[str, BaseAgent]:
        # Direct match by exact key name
        if step.preferred_agent not in ("any", "codex-worker", "claude-worker"):
            if step.preferred_agent in self.worker_agents:
                return step.preferred_agent, self.worker_agents[step.preferred_agent]

        # Always prefer Codex workers — keep Claude for orchestration only
        for name, agent in self.worker_agents.items():
            if "claude" not in name.lower():
                return name, agent

        # Fallback: use whatever is available
        name, agent = next(iter(self.worker_agents.items()))
        return name, agent

    @staticmethod
    def _extract_json(raw: str) -> dict:
        """Robustly extract a JSON object from an LLM response."""
        text = raw.strip()

        # Strip markdown code fences
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1].strip()

        # Find the outermost braces
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]

        return json.loads(text)


def _topo_sort(steps: list[Step]) -> list[Step]:
    """Return steps in dependency order via iterative DFS."""
    by_id = {s.step_id: s for s in steps}
    result: list[Step] = []
    visited: set[str] = set()

    def visit(step: Step) -> None:
        if step.step_id in visited:
            return
        for dep_id in step.depends_on:
            if dep := by_id.get(dep_id):
                visit(dep)
        visited.add(step.step_id)
        result.append(step)

    for s in steps:
        visit(s)
    return result
