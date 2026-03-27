from __future__ import annotations
import json
import uuid
import logging
from pathlib import Path
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
        if hasattr(self.agent, "chat_plan"):
            raw = self.agent.chat_plan(_SYSTEM, [{"role": "user", "content": msg}])
        else:
            raw = self.agent.chat(_SYSTEM, [{"role": "user", "content": msg}])
        if not raw or not raw.strip():
            raise ValueError("Orchestrator returned empty plan response — check Claude CLI connection")
        data = self._extract_json(raw)
        if not data.get("task_id"):
            data["task_id"] = str(uuid.uuid4())[:8]
        return Plan(**data)

    def review(self, step: Step, result: WorkerResult) -> Review:
        # Read actual file contents so Claude reviews real code, not just a summary.
        # Cap per-file at 3 KB and total file content at 10 KB.
        _PER_FILE = 3000
        _TOTAL_CAP = 10000
        file_sections: list[str] = []
        total = 0
        for fname in result.files_written:
            fpath = Path(self.work_dir) / fname
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = "<file not readable>"
            snippet = content[:_PER_FILE]
            truncated = len(content) > _PER_FILE
            header = f"--- {fname} ({len(content)} chars{', truncated' if truncated else ''}) ---"
            section = f"{header}\n{snippet}"
            file_sections.append(section)
            total += len(section)
            if total >= _TOTAL_CAP:
                file_sections.append("... (additional files omitted — total cap reached)")
                break

        if file_sections:
            files_block = "\n\n".join(file_sections)
        else:
            files_block = result.result_text[:3000] or "No output captured."

        msg = (
            f"Review this step result.\n\n"
            f"STEP:\n"
            f"  ID: {step.step_id}\n"
            f"  Title: {step.title}\n"
            f"  Type: {step.type}\n"
            f"  Expected Output: {step.expected_output}\n\n"
            f"FILES WRITTEN ({len(result.files_written)}):\n\n{files_block}\n\n"
            f"Return your review as JSON."
        )
        if hasattr(self.agent, "chat_review"):
            raw = self.agent.chat_review(_SYSTEM, [{"role": "user", "content": msg}])
        else:
            raw = self.agent.chat(_SYSTEM, [{"role": "user", "content": msg}])
        if not raw or not raw.strip():
            raise ValueError(f"Orchestrator returned empty review response for {step.step_id}")
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

        if not plan.steps:
            raise ValueError("Orchestrator returned an empty plan — no steps to execute.")

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
                fire("on_step_result", step, result, worker_name)
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
        if not self.worker_agents:
            raise RuntimeError("No worker agents available")

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

        # Fast path: raw is already valid JSON
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Strip markdown code fences — only when the text is fence-wrapped.
        # Do NOT split on backticks when text starts with "{": step descriptions
        # often contain {PRODUCT_NAME} or `file.py` patterns that would corrupt it.
        if not text.startswith("{"):
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in text:
                parts = text.split("```")
                if len(parts) >= 3:
                    text = parts[1].strip()

        # Scan every "{" in the text until one yields valid JSON.
        # This handles preamble content containing {PRODUCT} patterns before the plan.
        last_err: json.JSONDecodeError | None = None
        search_from = 0
        while True:
            start = text.find("{", search_from)
            if start < 0:
                break

            # Depth-count to find the matching "}" for this "{"
            depth = 0
            in_string = False
            escape_next = False
            end = -1
            for i, ch in enumerate(text[start:], start):
                if escape_next:
                    escape_next = False
                    continue
                if ch == "\\" and in_string:
                    escape_next = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

            if end < 0:
                # No matching "}" — the rest of the text is truncated
                break

            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError as e:
                last_err = e
                search_from = start + 1  # try next "{"

        if last_err:
            raise ValueError(f"Invalid JSON in response: {last_err}. Raw: {raw[:300]!r}")
        raise ValueError(f"No JSON object found in response: {raw[:300]!r}")


def _topo_sort(steps: list[Step]) -> list[Step]:
    """Return steps in dependency order via DFS with cycle detection."""
    by_id = {s.step_id: s for s in steps}
    result: list[Step] = []
    visited: set[str] = set()
    visiting: set[str] = set()  # current DFS path — detects cycles

    def visit(step: Step) -> None:
        if step.step_id in visited:
            return
        if step.step_id in visiting:
            raise ValueError(f"Circular dependency detected involving step '{step.step_id}'")
        visiting.add(step.step_id)
        for dep_id in step.depends_on:
            if dep := by_id.get(dep_id):
                visit(dep)
        visiting.discard(step.step_id)
        visited.add(step.step_id)
        result.append(step)

    for s in steps:
        visit(s)
    return result
