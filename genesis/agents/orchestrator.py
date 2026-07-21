from __future__ import annotations
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import json
import re
import uuid
import logging
from pathlib import Path
from threading import Lock
from typing import Callable, TYPE_CHECKING

from genesis.schemas.plan import Plan, Step
from genesis.schemas.review import Review
from genesis.agents.worker import Worker, WorkerResult
from genesis.agents.availability import is_exhaustion_error
from genesis.memory import MemoryManager
from genesis.git_ops import GitManager
from genesis.evidence import evaluate_acceptance_gates, evaluate_patch_evidence
from genesis.config import GenesisConfig
from genesis.palace import PalaceStore
from genesis.policy import ExecutionPolicy
from genesis.runtime import RuntimeStore
from genesis.scheduler import (
    DependencyScheduler,
    ScheduledStep,
    StepScope,
    declared_step_scope,
    infer_step_scope,
)
from genesis.verifier import CommandResult, Verifier, VerificationResult
from genesis.worktree import WorktreeManager, WorktreePatch

if TYPE_CHECKING:
    from genesis.agents.base import BaseAgent


def _make_worker(agent: BaseAgent, memory_summary: str, work_dir: str,
                 output_callback=None):
    """Return the right worker type for the given agent."""
    try:
        from genesis.agents.codex_cli import CodexCLIAgent
        from genesis.agents.codex_worker import CodexWorker
        if isinstance(agent, CodexCLIAgent):
            # Bind Codex to THIS step's worktree so it writes there (and the diff
            # is captured), not in the agent's original main-repo work_dir.
            bound = agent.for_work_dir(work_dir)
            return CodexWorker(bound, memory_summary, work_dir,
                               output_callback=output_callback)
    except ImportError:
        pass
    return Worker(agent, memory_summary, work_dir, output_callback=output_callback)

logger = logging.getLogger(__name__)


class AgentOutput(str):
    """A backwards-compatible output line carrying its parallel step context.

    It behaves exactly like ``str`` for existing observers while richer clients
    can attribute interleaved output and token usage to the correct worker.
    """

    def __new__(
        cls,
        value: object,
        *,
        step_id: str = "",
        worker_name: str = "",
    ) -> AgentOutput:
        instance = super().__new__(cls, str(value))
        instance.step_id = step_id
        instance.worker_name = worker_name
        return instance


def _worker_failure_policy(reason: str) -> tuple[bool, str]:
    """Classify whether another code-producing turn can plausibly help."""
    lowered = str(reason or "").lower()
    if is_exhaustion_error(lowered):
        return False, "capacity_wait"
    hard_markers = (
        "no space left on device",
        "disk full",
        "read-only file system",
        "access is denied",
        "permission denied",
        "authentication required",
        "login required",
        "not logged in",
        "api key is missing",
        "missing api key",
        "credentials not found",
        "unauthorized",
        "forbidden",
        "blocked by policy",
        "policy denied",
        "no worker agents available",
        "executable not found",
        "command not found",
    )
    if any(marker in lowered for marker in hard_markers):
        return False, "hard_failure"
    return True, ""


@dataclass
class _StepExecution:
    step: Step
    worker_name: str
    worktree_path: Path | None = None
    result: WorkerResult | None = None
    patch: WorktreePatch | None = None
    patch_id: str = ""
    patch_version: int = 0
    reviewed_patch_sha: str = ""
    review: Review | None = None
    verification: VerificationResult | None = None
    repair_attempts: int = 0
    repair_id: str = ""
    repair_outcome: str = ""
    repair_patch_sha: str = ""
    reviewer_name: str = ""
    failed_agent: str = ""
    failed_reason: str = ""
    memory_note: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.failed_reason)

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
      "file_scope": ["<paths this step is expected to change, or * for broad repo work>"],
      "expected_output": "<description of what success looks like>",
      "context_hint": "<optional: file paths, constraints, examples>"
    }
  ]
}

Planning rules:
- Use the fewest useful steps (usually 1–6); each must produce a real artifact.
- Maximize safe parallelism. Add depends_on only for a real data/build dependency,
  never merely because one step appears earlier in the plan.
- Split independent work into non-overlapping file_scope entries so multiple
  workers can run at once. Use ["*"] only when repo-wide writes are truly required.
- Fill file_scope with concrete files/directories whenever known.
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
- Act like a senior mentor: prefer needs_revision with complete, concrete repair
  instructions whenever the isolated patch can plausibly be corrected. Do not
  reject merely because several fixable defects remain.
"""

_REVIEW_SYSTEM = """\
You are Genesis Independent Reviewer - a senior engineer doing final acceptance.

You did not write the implementation. Review the step result against the stated
expected output, changed files, and diff. Be concrete and strict.

Return ONLY a JSON object:

{
  "step_id": "<id>",
  "verdict": "<approved|needs_revision|rejected>",
  "quality_score": <1-10>,
  "feedback": "<specific actionable feedback if not approved, else empty string>",
  "memory_note": "<1-2 factual sentences about what changed>",
  "should_retry": <true|false>,
  "suggested_revision": "<if retryable: exact implementation instructions>"
}

Rules:
- Approve only complete, runnable work that matches the step.
- Treat declared file scope as a planning and scheduling hint, not a hard
  acceptance boundary. Relevant supporting changes outside it are allowed.
- Judge against the full task, step description, dependency state, worker
  summary, changed-file manifest, and bounded patch evidence supplied.
- A truncated patch is not by itself grounds for rejection. Use the manifest,
  representative excerpts, and file samples; request a focused revision only
  when you can identify a concrete defect or missing artifact.
- Use needs_revision only when the worker can likely repair it with one focused retry.
- Use rejected for unsafe, unrelated, or fundamentally wrong work.
- Be a constructive mentor. Enumerate every concrete defect needed for the next
  attempt; reserve rejected for work that cannot safely or plausibly be repaired
  in the retained isolated worktree.
- Never approve syntax errors, broken imports, failing tests, placeholders, or unreviewable output.
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
        runtime: RuntimeStore | None = None,
        palace: PalaceStore | None = None,
        policy: ExecutionPolicy | None = None,
        co_brain: BaseAgent | None = None,
        chatroom=None,
        registry=None,
    ):
        self.agent = agent
        self.worker_agents = worker_agents
        self.memory = memory
        self.git = git
        self.config = config
        self.work_dir = work_dir
        self.runtime = runtime
        self.palace = palace
        self.policy = policy
        # Second brain for collaborative planning (Phase 2). When present and
        # enabled, the two brains debate before self.agent synthesizes the plan.
        self.co_brain = co_brain
        self.chatroom = chatroom
        # Account failover: exhausted (rate/usage/quota-limited) accounts are
        # skipped for a cooldown so another account takes over.
        from genesis.agents.availability import AccountRegistry
        cooldown = getattr(getattr(config, "failover", None), "cooldown_seconds", 900)
        self.registry = registry if registry is not None else AccountRegistry(cooldown)

    @staticmethod
    def _notify_callback(
        callback: Callable | None,
        name: str,
        *args,
        **kwargs,
    ) -> None:
        """Invoke an observer without allowing it to affect task execution."""
        if callback is None:
            return
        try:
            callback(*args, **kwargs)
        except Exception:
            logger.warning("Observer callback %s failed", name, exc_info=True)

    def _fire_callback(
        self,
        callbacks: dict[str, Callable] | None,
        name: str,
        *args,
        **kwargs,
    ) -> None:
        callback = (callbacks or {}).get(name)
        self._notify_callback(callback, name, *args, **kwargs)

    def _try_memory_write(
        self,
        operation: str,
        write: Callable[[], None],
        *,
        run_id: str = "",
        step_id: str = "",
    ) -> bool:
        """Persist supplemental Markdown memory without corrupting run state.

        The SQLite runtime journal remains authoritative. A full/read-only disk
        must be visible in that journal, but it must not turn already committed
        code back into a pending or failed step.
        """

        try:
            write()
            return True
        except Exception as exc:
            logger.error("Memory %s failed: %s", operation, exc, exc_info=True)
            if self.runtime and run_id:
                try:
                    self.runtime.record_event(
                        run_id,
                        "memory_write_failed",
                        step_id=step_id,
                        payload={"operation": operation, "error": str(exc)},
                    )
                except Exception:
                    logger.warning("Could not journal the memory write failure", exc_info=True)
            return False

    @staticmethod
    def _fit_memory_sections(
        markdown: str,
        palace: str,
        max_chars: int,
    ) -> tuple[str, str]:
        """Fit recent Markdown and relevant Palace memory into one real budget."""

        limit = max(0, int(max_chars))
        if limit == 0:
            return "", ""
        if not markdown:
            return "", palace[:limit]
        if not palace:
            return markdown[-limit:], ""

        separator_size = len("\n\n---\n\n")
        if limit <= separator_size:
            return markdown[-limit:], ""
        available = max(0, limit - separator_size)
        if len(markdown) + len(palace) <= available:
            return markdown, palace

        markdown_size = min(len(markdown), available // 2)
        palace_size = min(len(palace), available - markdown_size)
        remaining = available - markdown_size - palace_size
        if remaining and markdown_size < len(markdown):
            extra = min(remaining, len(markdown) - markdown_size)
            markdown_size += extra
            remaining -= extra
        if remaining and palace_size < len(palace):
            palace_size += min(remaining, len(palace) - palace_size)
        return (
            markdown[-markdown_size:] if markdown_size else "",
            palace[:palace_size] if palace_size else "",
        )

    # ── Public API ─────────────────────────────────────────────────────────

    def plan(self, task: str, on_status=None) -> Plan:
        def status(msg: str) -> None:
            self._notify_callback(on_status, "on_status", msg)

        memory_budget = max(0, int(self.config.memory.max_context_chars))
        try:
            mem = self.memory.get_summary(memory_budget)
        except Exception as exc:
            logger.warning("Markdown memory wakeup failed: %s", exc)
            mem = ""
        palace_mem = ""
        if memory_budget and self.palace and self.config.memory.palace_enabled:
            try:
                palace_mem = self.palace.wakeup_context(
                    task,
                    max_chars=memory_budget,
                    wing=str(Path(self.work_dir).resolve()),
                )
            except Exception as e:
                logger.warning("Palace wakeup failed: %s", e)
        mem, palace_mem = self._fit_memory_sections(mem, palace_mem, memory_budget)
        # Phase 2: let the two brains debate first; fold their agreed discussion
        # into the planning context so self.agent synthesizes the shared plan.
        discussion = self._run_brain_debate(task, context=mem, on_status=on_status)

        base_msg = (
            f"CURRENT MEMORY CONTEXT:\n{mem}\n\n---\n\n"
            f"RETRIEVED PALACE MEMORY:\n{palace_mem or 'No relevant palace memories.'}\n\n---\n\n"
            f"{discussion}"
            f"TASK TO PLAN:\n{task}\n\n"
            f"Return the plan as JSON. Output ONLY the JSON object — "
            f"start your response with {{ and end with }}. No preamble, no explanation."
        )

        status("Synthesizing the plan...")
        last_err: Exception | None = None
        for attempt in range(2):
            if attempt:
                status("Re-parsing the plan...")
            msg = base_msg if attempt == 0 else (
                base_msg + "\n\nIMPORTANT: your previous response could not be parsed. "
                "Output ONLY the raw JSON object. No prose, no markdown fences, no code blocks. "
                "Begin with { and end with }."
            )
            def _do_plan(agent):
                if hasattr(agent, "chat_plan"):
                    return agent.chat_plan(_SYSTEM, [{"role": "user", "content": msg}])
                return agent.chat(_SYSTEM, [{"role": "user", "content": msg}])

            raw = self._invoke(self._brain_candidates(self.agent), _do_plan)

            if not raw or not raw.strip():
                last_err = ValueError("Empty plan response from orchestrator")
                logger.warning("Empty plan response on attempt %d", attempt + 1)
                continue

            try:
                data = self._extract_json(raw)
            except ValueError as e:
                last_err = e
                logger.warning("Plan JSON parse failed (attempt %d): %s", attempt + 1, e)
                self._dump_debug("plan_raw", raw)
                continue

            if not data.get("task_id"):
                data["task_id"] = str(uuid.uuid4())[:8]
            return Plan(**data)

        raise ValueError(f"Could not parse plan after 2 attempts: {last_err}")

    def _run_brain_debate(self, task: str, context: str = "", on_status=None) -> str:
        """Run the two-brain debate (if a co-brain is available and enabled) and
        return an agreed-discussion block to fold into the plan prompt. Returns
        an empty string when collaboration is unavailable or fails, so planning
        degrades gracefully to the single-brain path."""
        collab_cfg = getattr(self.config, "collaboration", None)
        if self.co_brain is None or (collab_cfg is not None and not collab_cfg.enabled):
            return ""
        try:
            from genesis.agents.collaboration import BrainCollaboration

            max_rounds = collab_cfg.max_rounds if collab_cfg else 2
            collab = BrainCollaboration(
                self.agent, self.co_brain, chatroom=self.chatroom, max_rounds=max_rounds
            )
            result = collab.discuss(task, context=context, on_status=on_status)
        except Exception as e:
            logger.warning("Brain debate failed, falling back to single-brain plan: %s", e)
            return ""

        self._notify_callback(
            on_status,
            "on_status",
            "Brains reached consensus" if result.converged
            else "Round cap reached - arbitrating the plan",
        )

        if not result.transcript:
            return ""
        status = "reached consensus" if result.converged else "did not fully converge; arbitrate"
        return (
            f"BRAIN DEBATE ({result.rounds} round(s), {status}):\n"
            f"{result.transcript}\n\n"
            f"Synthesize the above discussion into the final plan.\n\n---\n\n"
        )

    def review(
        self,
        step: Step,
        result: WorkerResult,
        *,
        plan: Plan | None = None,
        run_id: str = "",
        work_dir: str | Path | None = None,
        diff_text: str | None = None,
    ) -> Review:
        # Read bounded file samples so the reviewer sees real code without a
        # generated file or binary artifact consuming its entire context window.
        _PER_FILE = 2500
        _TOTAL_CAP = 7500
        file_sections: list[str] = []
        total = 0
        review_dir = Path(work_dir or self.work_dir)
        for fname in result.files_written:
            fpath = review_dir / fname
            try:
                size = fpath.stat().st_size
                with fpath.open("r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read(_PER_FILE + 1)
            except OSError:
                size = 0
                content = "<file not readable>"
            snippet = content[:_PER_FILE]
            truncated = size > len(snippet)
            header = f"--- {fname} ({size} bytes{', sampled' if truncated else ''}) ---"
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

        raw_diff = (
            diff_text
            if diff_text is not None
            else self.git.diff_text(result.files_written, max_chars=24000)
        ) or "No git diff available."
        diff_block = self._bounded_diff(raw_diff, max_chars=16000)
        changed_manifest = self._bounded_manifest(result.files_written)
        run_context = self._run_context(plan, run_id, step, max_chars=8000)
        try:
            project_memory = self.memory.get_summary(
                max(0, min(6000, int(self.config.memory.max_context_chars)))
            )
        except Exception as exc:
            logger.warning("Markdown memory read failed during review: %s", exc)
            project_memory = ""
        worker_summary = (result.result_text or "No worker summary captured.")[:3000]
        evidence = getattr(result, "evidence", None) or {}
        patch_version = evidence.get("version", "unknown")
        patch_sha = evidence.get("patch_sha", "unknown")

        msg = (
            f"Review this step result.\n\n"
            f"SHARED RUN CONTEXT:\n{run_context}\n\n"
            f"SHARED PROJECT MEMORY:\n{project_memory}\n\n"
            f"STEP:\n"
            f"  ID: {step.step_id}\n"
            f"  Title: {step.title}\n"
            f"  Type: {step.type}\n"
            f"  Description: {step.description[:6000]}\n"
            f"  Declared File Scope (scheduling hint): "
            f"{', '.join(step.file_scope) if step.file_scope else 'None'}\n"
            f"  Expected Output: {step.expected_output[:3000]}\n"
            f"  Context Hint: {(step.context_hint or 'None')[:2000]}\n\n"
            f"AUTHORITATIVE PATCH VERSION:\n"
            f"  Version: {patch_version}\n"
            f"  Patch SHA: {patch_sha}\n"
            f"  This verdict applies only to this exact patch SHA.\n\n"
            f"WORKER SUMMARY:\n{worker_summary}\n\n"
            f"CHANGED-FILE MANIFEST ({len(result.files_written)}):\n"
            f"{changed_manifest}\n\n"
            f"FILES WRITTEN ({len(result.files_written)}):\n\n{files_block}\n\n"
            f"BOUNDED GIT DIFF ({len(raw_diff)} source chars):\n\n{diff_block}\n\n"
            f"Return your review as JSON."
        )
        def _do_review(agent):
            if hasattr(agent, "chat_review"):
                return agent.chat_review(_REVIEW_SYSTEM, [{"role": "user", "content": msg}])
            return agent.chat(_REVIEW_SYSTEM, [{"role": "user", "content": msg}])

        # Review on the independent reviewer, failing over to another account if
        # it is rate/usage limited.
        raw = self._invoke(self._brain_candidates(self._review_agent()), _do_review)
        if not raw or not raw.strip():
            raise ValueError(f"Reviewer returned empty review response for {step.step_id}")
        data = self._extract_json(raw)
        return Review(**data)

    @staticmethod
    def _bounded_manifest(paths: list[str], max_chars: int = 4000) -> str:
        if not paths:
            return "No files changed."
        manifest = "\n".join(f"- {path}" for path in paths)
        if len(manifest) <= max_chars:
            return manifest
        return manifest[:max_chars].rstrip() + "\n... (manifest truncated)"

    @staticmethod
    def _bounded_diff(diff_text: str, max_chars: int = 24000) -> str:
        """Keep representative patch evidence within reviewer context limits."""
        if len(diff_text) <= max_chars:
            return diff_text

        sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
        sections = [section for section in sections if section]
        if len(sections) <= 1:
            head = int(max_chars * 0.7)
            tail = max_chars - head - 100
            return (
                diff_text[:head]
                + "\n... (middle of oversized diff omitted) ...\n"
                + diff_text[-tail:]
            )

        # Sample both ends so large multi-file patches do not hide either the
        # main implementation or late test/config changes.
        chosen = sections if len(sections) <= 12 else sections[:8] + sections[-4:]
        per_section = max(600, (max_chars - 800) // len(chosen))
        excerpts: list[str] = []
        for section in chosen:
            if len(section) > per_section:
                section = section[:per_section].rstrip() + "\n... (file diff sampled)\n"
            excerpts.append(section)
        omitted = len(sections) - len(chosen)
        note = (
            f"\n... ({omitted} additional file diff(s) omitted; see manifest) ...\n"
            if omitted > 0 else ""
        )
        bounded = note.join(("\n".join(excerpts[:8]), "\n".join(excerpts[8:]))) if omitted else "\n".join(excerpts)
        return bounded[:max_chars]

    def _review_agent(self) -> BaseAgent:
        """The dedicated reviewer. Prefer the peer brain so review is an
        independent check rather than the plan synthesizer grading itself;
        fall back to the primary brain when only one is available."""
        return self.co_brain or self.agent

    def run_task(self, task: str, callbacks: dict[str, Callable] | None = None) -> None:
        if self.runtime:
            saved = self.runtime.find_reusable_run(task)
            if saved:
                payload = self.runtime.get_checkpoint(saved.run_id, "plan_created")
                if payload:
                    plan = Plan(**payload)
                    self._fire_callback(
                        callbacks,
                        "on_status",
                        f"Reusing saved plan {saved.run_id}; planning is already complete.",
                    )
                    if saved.status == "blocked":
                        retry_ids: set[str] = set()
                        for record in self.runtime.steps(saved.run_id):
                            if record.status == "blocked":
                                retry_ids.update(self._retry_step_ids(plan, record.step_id))
                        for step_id in retry_ids:
                            record = self.runtime.get_step(saved.run_id, step_id)
                            if record and record.status != "committed":
                                self.runtime.reset_step_for_retry(saved.run_id, step_id)
                    else:
                        self.runtime.update_run_status(saved.run_id, "running")
                    self._fire_callback(callbacks, "on_plan", plan)
                    self._execute_plan_isolated(plan, callbacks=callbacks)
                    return
        self._run_fresh_task(task, callbacks)

    def plan_and_save(self, task: str, on_status=None) -> Plan:
        """Create a durable plan preview, reusing one already saved for task."""
        if self.runtime:
            saved = self.runtime.find_reusable_run(task)
            if saved:
                payload = self.runtime.get_checkpoint(saved.run_id, "plan_created")
                if payload:
                    self._notify_callback(
                        on_status,
                        "on_status",
                        f"Reusing saved plan {saved.run_id}.",
                    )
                    return Plan(**payload)
        plan = self.plan(task, on_status=on_status)
        if not plan.steps:
            raise ValueError("Orchestrator returned an empty plan - no steps to execute.")
        self._save_plan(task, plan, status="planned")
        return plan

    def _run_fresh_task(
        self,
        task: str,
        callbacks: dict[str, Callable] | None = None,
    ) -> None:
        def fire(name: str, *args, **kwargs) -> None:
            self._fire_callback(callbacks, name, *args, **kwargs)

        fire("on_status", "Planning task...")
        plan = self.plan(task, on_status=lambda m: fire("on_status", m))
        if not plan.steps:
            raise ValueError("Orchestrator returned an empty plan - no steps to execute.")
        fire("on_plan", plan)

        self._save_plan(task, plan, status="running")
        self._execute_plan_isolated(plan, callbacks=callbacks)

    def _save_plan(self, task: str, plan: Plan, *, status: str) -> None:
        run_id = plan.task_id
        ordered_steps = _topo_sort(plan.steps)
        if self.runtime:
            self.runtime.start_run(
                task,
                run_id=run_id,
                metadata={
                    "estimated_steps": plan.estimated_steps,
                    "task_summary": plan.task_summary,
                    "plan_retained": True,
                },
            )
            self.runtime.checkpoint(run_id, "plan_created", payload=plan.model_dump())
            for step in ordered_steps:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="pending",
                    metadata={"step": step.model_dump()},
                )
            self.runtime.update_run_status(run_id, status)

        if self.config.memory.auto_append_plan:
            self._try_memory_write(
                "append_plan",
                lambda: self.memory.append_plan(plan),
                run_id=run_id,
            )
        self._palace_add(
            run_id=run_id,
            step_id="",
            closet="plans",
            kind="plan",
            title=f"Plan: {plan.task_summary}",
            content=json.dumps(plan.model_dump(), indent=2, ensure_ascii=False),
            status="planned",
        )

    def resume_task(
        self,
        run_id: str,
        callbacks: dict[str, Callable] | None = None,
        *,
        retry_step_id: str | None = None,
    ) -> None:
        if not self.runtime:
            raise RuntimeError("Runtime store is not configured; cannot resume.")
        plan_payload = self.runtime.get_checkpoint(run_id, "plan_created")
        if not plan_payload:
            raise RuntimeError(f"No stored plan found for run {run_id}.")
        plan = Plan(**plan_payload)
        if retry_step_id:
            for step_id in self._retry_step_ids(plan, retry_step_id):
                record = self.runtime.get_step(run_id, step_id)
                if record and record.status != "committed":
                    self.runtime.reset_step_for_retry(run_id, step_id)
        else:
            self.runtime.update_run_status(run_id, "running")
        self._fire_callback(callbacks, "on_plan", plan)
        self._execute_plan_isolated(plan, callbacks=callbacks)

    def _execute_plan_isolated(
        self,
        plan: Plan,
        *,
        callbacks: dict[str, Callable] | None = None,
    ) -> None:
        cb = callbacks or {}
        output_callback = cb.get("on_output")
        repair_observer = cb.get("on_repair")
        output_lock = Lock()
        worktree_lock = Lock()

        def fire(name: str, *args, **kwargs) -> None:
            self._fire_callback(callbacks, name, *args, **kwargs)

        def guarded_output(
            text: str,
            *,
            step_id: str = "",
            worker_name: str = "",
        ) -> None:
            if output_callback:
                payload = AgentOutput(
                    text,
                    step_id=step_id,
                    worker_name=worker_name,
                )
                with output_lock:
                    self._notify_callback(output_callback, "on_output", payload)

        if not self.config.git.auto_commit:
            raise RuntimeError(
                "Isolated execution requires git.auto_commit=true so each accepted "
                "step becomes the base for the next worktree."
            )
        if not self.worker_agents:
            raise RuntimeError("No worker agents available")

        run_id = plan.task_id
        steps = _topo_sort(plan.steps)
        scheduler = DependencyScheduler(steps)
        max_parallel = max(1, min(
            self.config.runtime.max_parallel_workers,
            len(self.worker_agents) or 1,
            len(steps) or 1,
        ))
        worktrees = WorktreeManager(self.work_dir)
        workspace_snapshot = worktrees.workspace_snapshot()
        if self.runtime:
            self.runtime.checkpoint(
                run_id,
                "workspace_preflight",
                payload=workspace_snapshot,
            )
        self._reconcile_integrated_steps(plan, worktrees, fire)
        if not self._complete_pending_integration_rollbacks(
            plan,
            worktrees,
            fire,
        ):
            reason = (
                "A journaled integration rollback could not be completed safely; "
                "main was left untouched and the run requires operator review."
            )
            if self.runtime:
                self.runtime.update_run_status(
                    run_id,
                    "blocked",
                    metadata={"reason": reason},
                )
            fire("on_status", reason)
            return
        fire(
            "on_status",
            "Workspace preflight: "
            f"{workspace_snapshot['tracked_count']} tracked, "
            f"{len(workspace_snapshot['dirty'])} dirty, "
            f"{len(workspace_snapshot['untracked'])} untracked, "
            f"{len(workspace_snapshot['ignored_source'])} ignored source file(s).",
        )
        checkpoint_sha = worktrees.prepare_main(
            ignore_paths=[self.config.memory.file, ".genesis/"]
        )
        if checkpoint_sha:
            fire(
                "on_status",
                f"Saved current project state as checkpoint {checkpoint_sha}.",
            )

        committed_ids: set[str] = set()
        blocked_ids: set[str] = set()
        for step in steps:
            record = self.runtime.get_step(run_id, step.step_id) if self.runtime else None
            if not record:
                continue
            if record.status == "committed":
                cleanup_pending = [
                    str(item)
                    for item in (
                        record.metadata.get("cleanup_pending_worktrees", [])
                        or []
                    )
                    if item
                ]
                if record.worktree_path and record.worktree_path not in cleanup_pending:
                    cleanup_pending.append(record.worktree_path)
                if cleanup_pending:
                    self._cleanup_step_worktrees(
                        run_id,
                        step,
                        cleanup_pending,
                        worktrees,
                    )
                committed_ids.add(step.step_id)
                fire("on_status", f"Skipping committed {step.step_id}")
            elif record.status == "blocked":
                blocked_ids.add(step.step_id)
            elif record.metadata.get("repair_state") in {
                "repair_exhausted",
                "repair_disabled",
                "hard_failure",
                "capacity_wait",
                "integration_failed",
            }:
                # Recover legacy/crash-window terminal repairs as blocked. An
                # operator retry resets this state and deliberately grants a
                # fresh budget; an ordinary resume must not do so implicitly.
                if self.runtime:
                    self.runtime.upsert_step(
                        run_id,
                        step.step_id,
                        title=step.title,
                        status="blocked",
                        metadata={"lease": "blocked"},
                    )
                blocked_ids.add(step.step_id)
            elif record.status in {"running", "reviewing", "verifying"}:
                if self.runtime:
                    self.runtime.upsert_step(
                        run_id,
                        step.step_id,
                        title=step.title,
                        status="pending",
                        metadata={"resumed_from": record.status},
                    )

        completed = len(committed_ids)
        active: dict[Future[_StepExecution], tuple[ScheduledStep, str]] = {}
        active_scopes: dict[str, StepScope] = {}
        active_workers: set[str] = set()
        fire(
            "on_status",
            f"Executing {len(steps)} steps with up to {max_parallel} parallel worker(s)...",
        )
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            while completed < len(steps):
                open_slots = max_parallel - len(active)
                available_workers = len(
                    self._eligible_worker_candidates(active_workers)
                )
                batch = scheduler.select_ready(
                    committed_ids=committed_ids,
                    unavailable_ids=blocked_ids | set(active_scopes),
                    active_scopes=active_scopes.values(),
                    limit=min(open_slots, available_workers),
                )
                for scheduled in batch:
                    step = scheduled.step
                    worker_name, worker_agent = self._assign_worker(step, unavailable=active_workers)
                    def step_output(
                        text: str,
                        *,
                        _step_id: str = step.step_id,
                        _worker_name: str = worker_name,
                    ) -> None:
                        guarded_output(
                            str(text),
                            step_id=getattr(text, "step_id", "") or _step_id,
                            worker_name=getattr(text, "worker_name", "") or _worker_name,
                        )

                    def step_repair(
                        event: dict,
                        *,
                        _step: Step = step,
                    ) -> None:
                        with output_lock:
                            self._fire_callback(
                                callbacks,
                                "on_repair",
                                _step,
                                event,
                            )

                    active_workers.add(worker_name)
                    active_scopes[step.step_id] = scheduled.scope
                    fire("on_step_start", step, completed + len(active), len(steps))
                    fire("on_worker_assigned", step, worker_name)
                    if self.runtime:
                        self.runtime.upsert_step(
                            run_id,
                            step.step_id,
                            title=step.title,
                            status="running",
                            worker=worker_name,
                            metadata=self._scope_metadata(scheduled.step, scheduled.scope, lease="active"),
                        )
                        self.runtime.record_event(
                            run_id,
                            "step_leased",
                            step_id=step.step_id,
                            payload={
                                "worker": worker_name,
                                "effective_scope": list(scheduled.scope.paths),
                                "scope_source": scheduled.scope.source,
                                "max_parallel": max_parallel,
                            },
                        )
                    future = executor.submit(
                        self._run_step_in_worktree,
                        run_id,
                        plan,
                        step,
                        worker_name,
                        worker_agent,
                        worktrees,
                        step_output,
                        worktree_lock,
                        step_repair if repair_observer else None,
                    )
                    active[future] = (scheduled, worker_name)

                if not active:
                    reason = (
                        "No runnable steps remain. Pending steps are waiting on "
                        "blocked dependencies or conservative file-scope locks."
                    )
                    if self.runtime:
                        self.runtime.update_run_status(
                            run_id,
                            "blocked",
                            metadata={
                                "completed_steps": completed,
                                "total_steps": len(steps),
                                "blocked_steps": sorted(blocked_ids),
                                "reason": reason,
                            },
                        )
                    fire("on_status", reason)
                    break

                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    scheduled, worker_name = active.pop(future)
                    step = scheduled.step
                    active_workers.discard(worker_name)
                    active_scopes.pop(step.step_id, None)
                    try:
                        execution = future.result()
                    except Exception as e:
                        logger.exception("Unhandled step execution error for %s", step.step_id)
                        execution = _StepExecution(
                            step=step,
                            worker_name=worker_name,
                            failed_agent="worker-runtime",
                            failed_reason=str(e),
                            memory_note=f"Execution failed: {e}",
                        )

                    if execution.result:
                        fire("on_step_result", step, execution.result, execution.worker_name)
                    if execution.review:
                        fire("on_review", step, execution.review)

                    if execution.failed:
                        self._block_step(
                            run_id,
                            step,
                            execution.failed_agent or execution.worker_name,
                            execution.memory_note or execution.failed_reason,
                            execution.failed_reason,
                            fire,
                        )
                        blocked_ids.add(step.step_id)
                        continue

                    if not execution.patch:
                        self._block_step(
                            run_id,
                            step,
                            "worker-runtime",
                            "Step completed without a captured patch.",
                            "missing patch",
                            fire,
                        )
                        blocked_ids.add(step.step_id)
                        continue

                    if (
                        execution.review is None
                        or execution.review.verdict != "approved"
                        or execution.reviewed_patch_sha != execution.patch.patch_sha
                    ):
                        reason = (
                            "Refusing to apply an unreviewed or stale patch: "
                            f"current={execution.patch.patch_sha}, "
                            f"reviewed={execution.reviewed_patch_sha or 'none'}."
                        )
                        self._block_step(
                            run_id,
                            step,
                            "review-integrity",
                            reason,
                            reason,
                            fire,
                        )
                        blocked_ids.add(step.step_id)
                        continue

                    try:
                        worktrees.apply_check(execution.patch.patch_text)
                        worktrees.apply_patch(execution.patch.patch_text)
                    except Exception as e:
                        reason = (
                            "Approved patch could not be applied to current main: "
                            f"{e}"
                        )
                        if self._schedule_integration_repair(
                            run_id,
                            step,
                            execution,
                            stage="integration_apply",
                            reason=reason,
                            worktrees=worktrees,
                            worktree_lock=worktree_lock,
                            fire=fire,
                            refresh_base=True,
                        ):
                            continue
                        repair_status = self._record_repair_outcome(
                            run_id,
                            step,
                            execution,
                            outcome="integration_failed",
                        )
                        if repair_status:
                            fire("on_status", repair_status)
                        self._block_step(
                            run_id,
                            step,
                            "git-apply",
                            f"Patch apply failed: {e}",
                            str(e),
                            fire,
                        )
                        blocked_ids.add(step.step_id)
                        continue

                    sha = self.git.commit_step(
                        step.step_id,
                        step.title,
                        paths=execution.patch.changed_files,
                        patch_sha=execution.patch.patch_sha,
                        run_id=run_id,
                    )
                    if not sha and execution.patch.has_changes:
                        # The commit command may have completed before its
                        # response/validation failed. Reconcile exact durable
                        # identity before touching the working tree.
                        sha = self.git.find_step_commit(
                            run_id,
                            step.step_id,
                            execution.patch.patch_sha,
                        ) or self.git.find_commit_by_patch(
                            execution.patch.patch_sha,
                            execution.patch.changed_files,
                        )
                    if not sha and execution.patch.has_changes:
                        base_reason = "Commit failed after applying approved patch."
                        # Journal the budgeted retry (or terminal state) before
                        # changing main again. A crash on either side of the
                        # rollback can then resume without granting a free turn.
                        repair_scheduled = self._schedule_integration_repair(
                            run_id,
                            step,
                            execution,
                            stage="integration_commit",
                            reason=base_reason,
                            worktrees=worktrees,
                            worktree_lock=worktree_lock,
                            fire=fire,
                            refresh_base=False,
                            rollback_pending=True,
                        )
                        repair_status = ""
                        if not repair_scheduled:
                            repair_status = self._record_repair_outcome(
                                run_id,
                                step,
                                execution,
                                outcome="integration_failed",
                            )
                            if self.runtime and not repair_status:
                                self.runtime.record_step_event(
                                    run_id,
                                    step.step_id,
                                    "integration_failure_pending_rollback",
                                    title=step.title,
                                    status="blocked",
                                    metadata={
                                        "repair_state": "integration_failed",
                                        "repair_stage": "integration_commit",
                                        "repair_attempts": execution.repair_attempts,
                                        "repair_budget": max(
                                            0,
                                            int(self.config.runtime.retry_budget),
                                        ),
                                        "draft_retained": True,
                                        "blocked_reason": base_reason,
                                        "lease": "blocked",
                                    },
                                    payload={
                                        "reason": base_reason,
                                        "patch_sha": execution.patch.patch_sha,
                                    },
                                )

                        rollback_error = ""
                        try:
                            worktrees.rollback_patch(
                                execution.patch.patch_text,
                                execution.patch.changed_files,
                            )
                        except Exception as exc:
                            rollback_error = f" Automatic rollback also failed: {exc}"
                            logger.exception(
                                "Could not roll back uncommitted patch for %s",
                                step.step_id,
                            )
                        reason = base_reason + rollback_error
                        if repair_scheduled and not rollback_error:
                            if self.runtime:
                                try:
                                    self.runtime.record_step_event(
                                        run_id,
                                        step.step_id,
                                        "integration_rollback_completed",
                                        title=step.title,
                                        status="repairing",
                                        metadata={
                                            "repair_state": "validating",
                                        },
                                        payload={
                                            "patch_sha": execution.patch.patch_sha,
                                        },
                                    )
                                except Exception:
                                    # The persisted rollback-pending phase is
                                    # itself resumable, so this remains safe.
                                    logger.exception(
                                        "Could not record completed rollback for %s",
                                        step.step_id,
                                    )
                            continue
                        if repair_scheduled:
                            repair_status = self._record_repair_outcome(
                                run_id,
                                step,
                                execution,
                                outcome="integration_failed",
                            )
                        if repair_status:
                            fire("on_status", repair_status)
                        self._block_step(
                            run_id,
                            step,
                            "git",
                            reason,
                            reason,
                            fire,
                        )
                        blocked_ids.add(step.step_id)
                        continue
                    retained_cleanup: list[str] = []
                    if self.runtime:
                        integrated_record = self.runtime.get_step(
                            run_id, step.step_id
                        )
                        if integrated_record:
                            retained_cleanup = [
                                str(item)
                                for item in (
                                    integrated_record.metadata.get(
                                        "retained_worktrees", []
                                    )
                                    or []
                                )
                                if item
                            ]
                    cleanup_paths = list(dict.fromkeys(
                        item
                        for item in [
                            *retained_cleanup,
                            str(execution.worktree_path or ""),
                        ]
                        if item
                    ))
                    (
                        repair_event_type,
                        repair_metadata,
                        repair_event_payload,
                        repair_status,
                    ) = self._repair_outcome_details(step, execution)
                    if self.runtime:
                        committed_metadata = self._scope_metadata(
                            step,
                            scheduled.scope,
                            lease="released",
                            extra={
                                "reviewer": execution.reviewer_name,
                                "review_verdict": execution.review.verdict if execution.review else "",
                                "current_patch_sha": execution.patch.patch_sha,
                                "reviewed_patch_sha": execution.reviewed_patch_sha,
                                "review_state": "committed",
                                "repair_attempts": execution.repair_attempts,
                                "retained_worktrees": [],
                                "cleanup_pending_worktrees": cleanup_paths,
                            },
                        )
                        committed_metadata.update(repair_metadata)
                        self.runtime.upsert_step(
                            run_id,
                            step.step_id,
                            title=step.title,
                            status="committed",
                            commit_sha=sha or "",
                            metadata=committed_metadata,
                            event_type=repair_event_type,
                            event_payload=repair_event_payload,
                        )
                    if repair_status:
                        fire("on_status", repair_status)
                    self._try_memory_write(
                        "append_step",
                        lambda: self.memory.append_step(
                            step.step_id,
                            step.title,
                            execution.worker_name,
                            execution.review.memory_note,
                            execution.review.verdict,
                        ),
                        run_id=run_id,
                        step_id=step.step_id,
                    )
                    self._remember_step(
                        run_id,
                        step,
                        execution.worker_name,
                        execution.review,
                        execution.patch,
                        patch_artifact_id=execution.patch_id,
                        commit_sha=sha or "",
                    )
                    # External publication and observers happen only after the
                    # durable committed transition has been recorded.
                    if sha and self.config.git.auto_push:
                        self.git.push()
                    fire("on_commit", step, sha)
                    self._cleanup_step_worktrees(
                        run_id,
                        step,
                        cleanup_paths,
                        worktrees,
                    )

                    committed_ids.add(step.step_id)
                    completed += 1
                    fire("on_step_complete", step, execution.review, completed, len(steps))

        if completed == len(steps) and not blocked_ids:
            release_summary = self._release_summary(plan, completed, len(steps))
            self._try_memory_write(
                "complete_task",
                lambda: self.memory.complete_task(plan.task_id),
                run_id=run_id,
            )
            if self.runtime:
                self.runtime.record_event(
                    run_id,
                    "release_summary",
                    payload={"summary": release_summary},
                )
                self.runtime.update_run_status(
                    run_id,
                    "completed",
                    metadata={
                        "completed_steps": completed,
                        "total_steps": len(steps),
                        "release_summary": release_summary,
                    },
                )
            self._palace_add(
                run_id=run_id,
                step_id="",
                closet="runs",
                kind="run-summary",
                title=f"Completed: {plan.task_summary}",
                content=release_summary,
                status="completed",
            )
            sha = self.git.commit_step("task-complete", plan.task_summary[:60])
            if sha and self.config.git.auto_push:
                self.git.push()
            fire("on_task_complete", plan)
        elif self.runtime:
            self.runtime.update_run_status(
                run_id,
                "blocked",
                metadata={
                    "completed_steps": completed,
                    "total_steps": len(steps),
                    "blocked_steps": sorted(blocked_ids),
                },
            )

    def _reconcile_integrated_steps(
        self,
        plan: Plan,
        worktrees: WorktreeManager,
        fire: Callable,
    ) -> None:
        """Close the Git-to-runtime crash gap using exact reviewed bytes."""
        if not self.runtime or not self.git:
            return
        for step in plan.steps:
            record = self.runtime.get_step(plan.task_id, step.step_id)
            if not record or record.status == "committed" or not record.worktree_path:
                continue
            metadata = record.metadata
            current_patch_sha = str(metadata.get("current_patch_sha", "") or "")
            reviewed_patch_sha = str(metadata.get("reviewed_patch_sha", "") or "")
            if not current_patch_sha or reviewed_patch_sha != current_patch_sha:
                continue
            try:
                review = Review(**record.review_json)
            except Exception:
                continue
            if review.step_id != step.step_id or review.verdict != "approved":
                continue
            verification_data = record.verification_json
            verified_patch_sha = str(
                metadata.get("verified_patch_sha", "") or ""
            )
            verification_patch_sha = str(
                verification_data.get("verified_patch_sha", "") or ""
            )
            if (
                verified_patch_sha != current_patch_sha
                or verification_patch_sha != current_patch_sha
            ):
                # A pass/failure belongs only to the immutable bytes the
                # verifier observed. Never reuse a prior candidate's result
                # to close a Git/runtime crash gap for replacement bytes.
                continue
            verification_passed = bool(verification_data.get("passed", False))
            if (
                not verification_passed
                and self.config.verification.require_for_commit
            ):
                continue

            worktree_path = Path(record.worktree_path)
            if not worktree_path.exists():
                continue
            try:
                patch = worktrees.capture_patch(worktree_path, step)
                if (
                    patch.patch_sha != current_patch_sha
                    or not patch.has_changes
                    or not worktrees.candidate_matches_main(
                        worktree_path,
                        patch.changed_files,
                    )
                ):
                    continue
            except Exception as exc:
                logger.warning(
                    "Could not inspect integration recovery for %s: %s",
                    step.step_id,
                    exc,
                )
                continue

            sha = self.git.find_step_commit(
                plan.task_id,
                step.step_id,
                patch.patch_sha,
            )
            if not sha:
                # The process may have died after applying but before commit.
                # Exact candidate equality lets us finish that reviewed commit
                # without staging any unrelated paths.
                sha = self.git.commit_step(
                    step.step_id,
                    step.title,
                    paths=patch.changed_files,
                    patch_sha=patch.patch_sha,
                    run_id=plan.task_id,
                )
            if not sha:
                # Compatibility for an older restart that checkpointed the
                # already-applied bytes before this reconciliation existed.
                sha = self.git.find_step_commit(
                    plan.task_id,
                    step.step_id,
                    patch.patch_sha,
                ) or self.git.find_commit_by_patch(
                    patch.patch_sha,
                    patch.changed_files,
                )
            if not sha:
                continue

            try:
                repair_attempts = max(
                    0, int(metadata.get("repair_attempts", 0) or 0)
                )
            except (TypeError, ValueError):
                repair_attempts = 0
            command_results = [
                CommandResult(
                    command=str(item.get("command", "")),
                    returncode=int(item.get("returncode", 0) or 0),
                    output=str(item.get("output", "")),
                )
                for item in verification_data.get("commands", [])
                if isinstance(item, dict)
            ]
            verification = VerificationResult(
                passed=verification_passed,
                skipped=bool(verification_data.get("skipped", False)),
                reason=str(verification_data.get("reason", "") or ""),
                commands=command_results,
                failure_kind=str(
                    verification_data.get("failure_kind", "") or ""
                ),
                repairable=bool(verification_data.get("repairable", True)),
            )
            execution = _StepExecution(
                step=step,
                worker_name=record.worker,
                worktree_path=worktree_path,
                patch=patch,
                patch_id=record.patch_artifact_id,
                patch_version=int(metadata.get("current_patch_version", 0) or 0),
                reviewed_patch_sha=reviewed_patch_sha,
                review=review,
                verification=verification,
                repair_attempts=repair_attempts,
                repair_id=str(metadata.get("repair_id", "") or ""),
                repair_outcome=(
                    "verified" if verification_passed else "accepted_advisory"
                ),
                repair_patch_sha=patch.patch_sha,
                reviewer_name=str(metadata.get("reviewer", "") or ""),
            )
            (
                repair_event_type,
                repair_metadata,
                repair_event_payload,
                _,
            ) = self._repair_outcome_details(step, execution)
            reconciled_cleanup = list(dict.fromkeys(
                item
                for item in [
                    *[
                        str(value)
                        for value in (
                            metadata.get("retained_worktrees", []) or []
                        )
                        if value
                    ],
                    str(worktree_path),
                ]
                if item
            ))
            reconciled_metadata: dict[str, object] = {
                "current_patch_sha": patch.patch_sha,
                "reviewed_patch_sha": patch.patch_sha,
                "review_state": "committed",
                "repair_attempts": repair_attempts,
                "reconciled_integration": True,
                "lease": "released",
                "retained_worktrees": [],
                "cleanup_pending_worktrees": reconciled_cleanup,
            }
            reconciled_metadata.update(repair_metadata)
            self.runtime.upsert_step(
                plan.task_id,
                step.step_id,
                title=step.title,
                status="committed",
                commit_sha=sha,
                metadata=reconciled_metadata,
                event_type=repair_event_type or "integration_reconciled",
                event_payload=(
                    repair_event_payload
                    if repair_event_type
                    else {
                        "patch_sha": patch.patch_sha,
                        "commit_sha": sha,
                    }
                ),
            )
            if repair_event_type:
                self.runtime.record_event(
                    plan.task_id,
                    "integration_reconciled",
                    step_id=step.step_id,
                    payload={
                        "patch_sha": patch.patch_sha,
                        "commit_sha": sha,
                        "repair_attempts": repair_attempts,
                    },
                )
            self._try_memory_write(
                "append_reconciled_step",
                lambda: self.memory.append_step(
                    step.step_id,
                    step.title,
                    record.worker,
                    review.memory_note,
                    review.verdict,
                ),
                run_id=plan.task_id,
                step_id=step.step_id,
            )
            self._remember_step(
                plan.task_id,
                step,
                record.worker,
                review,
                patch,
                patch_artifact_id=record.patch_artifact_id,
                commit_sha=sha,
            )
            if self.config.git.auto_push:
                self.git.push()
            self._cleanup_step_worktrees(
                plan.task_id,
                step,
                reconciled_cleanup,
                worktrees,
            )
            fire(
                "on_status",
                f"Reconciled already-integrated {step.step_id} at commit {sha}.",
            )
            fire("on_commit", step, sha)

    def _complete_pending_integration_rollbacks(
        self,
        plan: Plan,
        worktrees: WorktreeManager,
        fire: Callable,
    ) -> bool:
        """Finish a pre-journaled rollback before workspace checkpointing."""
        if not self.runtime or not self.git:
            return True

        all_safe = True
        for step in plan.steps:
            record = self.runtime.get_step(plan.task_id, step.step_id)
            if (
                not record
                or record.status == "committed"
                or record.metadata.get("repair_state")
                != "integration_rollback_pending"
            ):
                continue

            def fail_pending(reason: str) -> None:
                nonlocal all_safe
                all_safe = False
                try:
                    self.runtime.record_step_event(
                        plan.task_id,
                        step.step_id,
                        "integration_rollback_recovery_failed",
                        title=step.title,
                        status="blocked",
                        metadata={
                            "repair_state": "integration_failed",
                            "repair_stage": "integration_commit",
                            "blocked_reason": reason,
                            "draft_retained": True,
                            "lease": "blocked",
                        },
                        payload={"reason": reason},
                    )
                except Exception:
                    logger.exception(
                        "Could not persist rollback recovery failure for %s",
                        step.step_id,
                    )
                fire("on_error", step, reason)

            if not record.worktree_path:
                fail_pending(
                    "Cannot finish the journaled integration rollback because "
                    "the retained worktree path is missing."
                )
                continue
            worktree_path = Path(record.worktree_path)
            if not worktree_path.exists():
                fail_pending(
                    "Cannot finish the journaled integration rollback because "
                    f"the retained worktree no longer exists: {worktree_path}"
                )
                continue
            try:
                patch = worktrees.capture_patch(worktree_path, step)
                expected_sha = str(
                    record.metadata.get("current_patch_sha", "") or ""
                )
                if (
                    not patch.has_changes
                    or not expected_sha
                    or patch.patch_sha != expected_sha
                ):
                    raise RuntimeError(
                        "retained candidate identity changed before rollback recovery"
                    )

                # A clean HEAD delta means rollback already completed (or the
                # exact result is durably in HEAD). Never reverse committed
                # bytes merely because they equal the retained candidate.
                if not worktrees.main_matches_base(
                    worktree_path,
                    patch.changed_files,
                ):
                    if not worktrees.candidate_matches_main(
                        worktree_path,
                        patch.changed_files,
                    ):
                        raise RuntimeError(
                            "main contains changes that are neither HEAD nor the "
                            "exact reviewed candidate"
                        )
                    worktrees.rollback_patch(
                        patch.patch_text,
                        patch.changed_files,
                    )
                    if not worktrees.main_matches_base(
                        worktree_path,
                        patch.changed_files,
                    ):
                        raise RuntimeError(
                            "reviewed paths still differ from HEAD after rollback"
                        )

                self.runtime.record_step_event(
                    plan.task_id,
                    step.step_id,
                    "integration_rollback_recovered",
                    title=step.title,
                    status="repairing",
                    metadata={"repair_state": "validating"},
                    payload={"patch_sha": patch.patch_sha},
                )
                fire(
                    "on_status",
                    f"Recovered journaled rollback for {step.step_id}; "
                    "revalidating the retained candidate.",
                )
            except Exception as exc:
                fail_pending(
                    "Could not safely finish journaled integration rollback: "
                    f"{exc}"
                )
        return all_safe

    def _run_step_in_worktree(
        self,
        run_id: str,
        plan: Plan,
        step: Step,
        worker_name: str,
        worker_agent: BaseAgent,
        worktrees: WorktreeManager,
        output_callback: Callable[[str], None] | None,
        worktree_lock,
        repair_callback: Callable[[dict], None] | None = None,
    ) -> _StepExecution:
        execution = _StepExecution(step=step, worker_name=worker_name)
        execution.reviewer_name = self._reviewer_name()
        try:
            record = self.runtime.get_step(run_id, step.step_id) if self.runtime else None
            worktree_path = Path(record.worktree_path) if record and record.worktree_path else None
            if not worktree_path or not worktree_path.exists():
                with worktree_lock:
                    worktree_path = worktrees.create(run_id, step.step_id)
                    overlaid = worktrees.materialize_referenced_ignored(
                        worktree_path,
                        step,
                    )
                    if overlaid and self.runtime:
                        self.runtime.record_event(
                            run_id,
                            "workspace_overlay",
                            step_id=step.step_id,
                            payload={"files": overlaid},
                        )
            execution.worktree_path = worktree_path
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="running",
                    worker=worker_name,
                    worktree_path=str(worktree_path),
                )

            specialty = self._specialty_for(step)
            step_room = self._open_step_room(step, worker_name, specialty)
            # Failover state: the account actually used may change mid-step if the
            # current one hits its rate/usage limit.
            worker_state = {"name": worker_name, "agent": worker_agent}
            base_output_callback = output_callback

            def contextual_output(text: str) -> None:
                if base_output_callback:
                    base_output_callback(AgentOutput(
                        text,
                        step_id=step.step_id,
                        worker_name=worker_state["name"],
                    ))

            output_callback = contextual_output if base_output_callback else None
            turn_version = 0
            # Run context does not change during implementation turns; revision
            # instructions travel in the revised Step itself. Reusing this
            # bounded snapshot avoids rereading Markdown and querying FTS for
            # every dialogue/retry turn.
            step_memory = self._step_memory(step, plan, run_id)
            record_metadata = dict(record.metadata) if record else {}
            try:
                turn_version = max(
                    0, int(record_metadata.get("current_patch_version", 0) or 0)
                )
            except (TypeError, ValueError):
                turn_version = 0
            repair_phase = {
                "state": str(record_metadata.get("repair_state", "") or ""),
                "stage": str(record_metadata.get("repair_stage", "") or ""),
                "id": str(record_metadata.get("repair_id", "") or ""),
                "prior_patch_sha": str(
                    record_metadata.get("repair_prior_patch_sha", "") or ""
                ),
                "reason": str(record_metadata.get("last_repair_reason", "") or ""),
            }
            try:
                legacy_repair_attempts = int(
                    record_metadata.get("repair_attempts", 0) or 0
                )
            except (TypeError, ValueError):
                legacy_repair_attempts = 0
            if (
                not repair_phase["state"]
                and legacy_repair_attempts > 0
            ):
                # Legacy/in-flight repair rows predate explicit phases. Treat
                # them as already executing so the scheduler's running lease
                # cannot accidentally grant a fresh, unbudgeted worker turn.
                repair_phase["state"] = "executing"
            execution.repair_id = repair_phase["id"]

            def set_repair_phase(
                state: str,
                event_type: str,
                *,
                patch_sha: str = "",
            ) -> None:
                repair_phase["state"] = state
                if not self.runtime or not repair_phase["id"]:
                    return
                metadata: dict[str, object] = {"repair_state": state}
                if patch_sha:
                    metadata["repair_candidate_patch_sha"] = patch_sha
                self.runtime.record_step_event(
                    run_id,
                    step.step_id,
                    event_type,
                    title=step.title,
                    metadata=metadata,
                    payload={
                        "repair_id": repair_phase["id"],
                        "stage": repair_phase["stage"],
                        "patch_sha": patch_sha,
                    },
                )

            def run_turn(s):
                nonlocal turn_version
                if repair_phase["state"] == "scheduled":
                    set_repair_phase("executing", "repair_worker_started")
                result = self._worker_execute_with_failover(
                    s,
                    worktree_path,
                    output_callback,
                    worker_state,
                    step_room,
                    plan=plan,
                    run_id=run_id,
                    memory_summary=step_memory,
                )
                if result is not None and result.success:
                    turn_version += 1
                    reported_files = list(result.files_written or [])
                    turn_patch = worktrees.capture_patch(worktree_path, step)
                    result.files_written = turn_patch.changed_files
                    result.evidence = self._turn_evidence(
                        turn_patch,
                        turn_version,
                        step=step,
                        reported_files=reported_files,
                        work_dir=worktree_path,
                    )
                    self._record_patch_version(
                        run_id,
                        step,
                        turn_patch,
                        turn_version,
                    )
                    if repair_phase["state"] == "executing":
                        set_repair_phase(
                            "candidate_ready",
                            "repair_candidate_ready",
                            patch_sha=turn_patch.patch_sha,
                        )
                    if self.runtime:
                        self.runtime.record_event(
                            run_id,
                            "worker_turn_evidence",
                            step_id=step.step_id,
                            payload={
                                key: value
                                for key, value in result.evidence.items()
                                if key != "patch_text"
                            },
                        )
                return result

            # A single shared budget covers every automatic worker repair in
            # this execution generation. Crash/resume keeps the persisted count;
            # an explicit operator retry resets it in RuntimeStore.
            repair_budget = max(0, int(self.config.runtime.retry_budget))
            try:
                persisted_repairs = int(
                    (record.metadata if record else {}).get("repair_attempts", 0) or 0
                )
            except (TypeError, ValueError):
                persisted_repairs = 0
            execution.repair_attempts = max(0, persisted_repairs)
            repairs_left = max(0, repair_budget - execution.repair_attempts)
            brain_name = getattr(self.agent, "name", "brain") or "brain"
            last_repair_trigger = {"stage": "", "reason": ""}

            def capture_candidate(candidate_result: WorkerResult) -> WorktreePatch:
                # Acceptance activation is anchored to the immutable original
                # step. Repair feedback must never create a new keyword gate.
                candidate_patch = worktrees.capture_patch(worktree_path, step)
                candidate_result.files_written = candidate_patch.changed_files
                execution.result = candidate_result
                execution.patch = candidate_patch
                execution.patch_version = turn_version
                execution.patch_id = self._store_patch(
                    run_id, step.step_id, candidate_patch
                )
                execution.worker_name = worker_state["name"]
                if self.runtime:
                    self.runtime.upsert_step(
                        run_id,
                        step.step_id,
                        title=step.title,
                        status="reviewing",
                        worker=worker_state["name"],
                        patch_artifact_id=execution.patch_id,
                        metadata={
                            "changed_files": candidate_patch.changed_files,
                            "repair_attempts": execution.repair_attempts,
                            "repair_budget": repair_budget,
                            "repairs_remaining": repairs_left,
                        },
                    )
                    self.runtime.record_event(
                        run_id,
                        "worker_finished",
                        step_id=step.step_id,
                        payload={
                            "worker": worker_state["name"],
                            "changed_files": candidate_patch.changed_files,
                            "repair_attempts": execution.repair_attempts,
                        },
                    )
                return candidate_patch

            def consume_repair(
                stage: str,
                reason: str,
                *,
                prior_patch_sha: str = "",
                invalidate_candidate: bool = True,
                worker_turn: bool = True,
            ) -> dict[str, object] | None:
                nonlocal repairs_left
                if repairs_left <= 0:
                    return None

                repairs_left -= 1
                execution.repair_attempts += 1
                attempt = execution.repair_attempts
                bounded_reason = str(reason or "unknown failure").strip()[:6000]
                last_repair_trigger["stage"] = stage
                last_repair_trigger["reason"] = bounded_reason
                repair_event = self._record_repair_attempt(
                    run_id,
                    step,
                    reason=bounded_reason,
                    attempts_used=attempt,
                    attempts_left=repairs_left,
                    stage=stage,
                    budget_total=repair_budget,
                    prior_patch_sha=prior_patch_sha,
                    worker=worker_state["name"],
                    repair_state="scheduled" if worker_turn else "validating",
                )
                execution.repair_id = str(repair_event.get("repair_id", "") or "")
                repair_phase.update({
                    "state": "scheduled" if worker_turn else "validating",
                    "stage": stage,
                    "id": execution.repair_id,
                    "prior_patch_sha": prior_patch_sha,
                    "reason": bounded_reason,
                })
                status = (
                    f"REPAIR {attempt}/{repair_budget} | {step.step_id} | {stage}\n"
                    f"{bounded_reason}"
                )
                if repair_callback and repair_event:
                    repair_callback(repair_event)
                elif output_callback:
                    output_callback(status)
                self._post_step(
                    step_room, brain_name, "brain", status, "status"
                )

                if invalidate_candidate:
                    execution.review = None
                    execution.reviewed_patch_sha = ""
                    execution.verification = None
                return repair_event

            def start_repair(
                stage: str,
                reason: str,
                *,
                prior_patch_sha: str = "",
            ) -> tuple[Step, WorkerResult | None, str] | None:
                repair_event = consume_repair(
                    stage,
                    reason,
                    prior_patch_sha=prior_patch_sha,
                )
                if repair_event is None:
                    return None

                bounded_reason = str(reason or "unknown failure").strip()[:6000]

                repair_instruction = (
                    "Continue in the existing isolated worktree. Preserve correct "
                    "work already present, diagnose the evidence below, and make the "
                    "smallest complete correction. Do not merely describe the fix; "
                    "edit the files and verify the result."
                    f"\n\nObserved {stage} failure:\n{bounded_reason}"
                )
                revised_step = self._revision_step(step, repair_instruction)
                repaired_result = run_turn(revised_step)
                execution.worker_name = worker_state["name"]
                return revised_step, repaired_result, prior_patch_sha

            def terminal_failure(
                stage: str,
                reason: str,
                *,
                repairable: bool,
                agent: str | None = None,
                block_kind_hint: str = "",
            ) -> _StepExecution:
                base_reason = str(reason or "unknown failure").strip()
                retained = (
                    f" Draft retained at {worktree_path} and was not applied."
                    if worktree_path
                    else " Draft was not applied."
                )
                if block_kind_hint == "capacity_wait":
                    block_kind = "capacity_wait"
                    guidance = (
                        " Automatic repair is waiting for worker capacity; "
                        "retry after the account cooldown or login is restored."
                    )
                elif repairable and repair_budget <= 0:
                    block_kind = "repair_disabled"
                    guidance = " Automatic self-repair is disabled (retry_budget=0)."
                elif repairable:
                    block_kind = "repair_exhausted"
                    guidance = (
                        " Automatic self-repair budget exhausted "
                        f"({execution.repair_attempts}/{repair_budget} used)."
                    )
                else:
                    block_kind = "hard_failure"
                    guidance = (
                        " Automatic self-repair was skipped for this "
                        "non-repairable failure."
                    )

                full_reason = base_reason + guidance + retained
                execution.failed_agent = agent or worker_state["name"]
                execution.failed_reason = full_reason
                execution.memory_note = full_reason
                if self.runtime:
                    event_type = (
                        "repair_budget_exhausted"
                        if block_kind == "repair_exhausted"
                        else "repair_deferred"
                        if block_kind == "capacity_wait"
                        else "repair_skipped"
                    )
                    self.runtime.record_step_event(
                        run_id,
                        step.step_id,
                        event_type,
                        title=step.title,
                        status="blocked",
                        metadata={
                            "last_failure_stage": stage,
                            "block_kind": block_kind,
                            "repair_attempts": execution.repair_attempts,
                            "repair_budget": repair_budget,
                            "repairs_remaining": repairs_left,
                            "repair_state": block_kind,
                            "repair_stage": stage,
                            "draft_retained": bool(worktree_path),
                            "blocked_reason": full_reason,
                            "lease": "blocked",
                            "resolution": (
                                "retry_after_cooldown"
                                if block_kind == "capacity_wait"
                                else "manual_retry"
                                if block_kind in {"repair_exhausted", "repair_disabled"}
                                else "operator_action"
                            ),
                        },
                        payload={
                            "stage": stage,
                            "block_kind": block_kind,
                            "reason": base_reason,
                            "attempts_used": execution.repair_attempts,
                            "budget_total": repair_budget,
                            "worktree_path": str(worktree_path or ""),
                        },
                    )
                label = (
                    "AUTO-REPAIR EXHAUSTED"
                    if block_kind == "repair_exhausted"
                    else "AUTO-REPAIR DISABLED"
                    if block_kind == "repair_disabled"
                    else "WAITING FOR CAPACITY"
                    if block_kind == "capacity_wait"
                    else "HARD BLOCK"
                )
                self._post_step(
                    step_room,
                    brain_name,
                    "brain",
                    f"{label} | {step.step_id} | {stage}\n{full_reason}",
                    "status",
                )
                return execution

            def stage_repair_outcome(
                final_patch: WorktreePatch,
                outcome: str = "verified",
            ) -> None:
                # The candidate is not resolved until its reviewed patch has
                # been integrated and durably committed on the main worktree.
                if execution.repair_attempts <= 0:
                    return
                execution.repair_outcome = outcome
                execution.repair_patch_sha = final_patch.patch_sha

            def prepare_candidate(
                candidate_step: Step,
                candidate_result: WorkerResult | None,
                *,
                must_change_from: str = "",
            ) -> tuple[
                Step,
                WorkerResult | None,
                WorktreePatch | None,
                dict[str, object] | None,
            ]:
                required_change = must_change_from
                current_step = candidate_step
                current_result = candidate_result

                while True:
                    if current_result is None or not getattr(
                        current_result, "success", False
                    ):
                        reason = (
                            (getattr(current_result, "error", "") if current_result else "")
                            or "Worker failed without returning a result."
                        )
                        repairable, block_kind = _worker_failure_policy(reason)
                        repair = (
                            start_repair(
                                "worker",
                                f"Worker execution failed: {reason}",
                                prior_patch_sha=required_change,
                            )
                            if repairable
                            else None
                        )
                        if repair is not None:
                            current_step, current_result, required_change = repair
                            continue
                        return current_step, current_result, None, {
                            "stage": "worker",
                            "reason": f"Worker execution failed: {reason}",
                            "repairable": repairable,
                            "block_kind": block_kind,
                        }

                    candidate_patch = capture_candidate(current_result)

                    if not candidate_patch.has_changes:
                        reason = (
                            "Worker completed without a reviewable patch. "
                            "The authoritative changed-file manifest is empty."
                        )
                        repair = start_repair(
                            "patch",
                            reason,
                            prior_patch_sha=candidate_patch.patch_sha,
                        )
                        if repair is not None:
                            current_step, current_result, required_change = repair
                            continue
                        return current_step, current_result, candidate_patch, {
                            "stage": "patch",
                            "reason": reason,
                            "repairable": True,
                        }

                    if (
                        required_change
                        and candidate_patch.patch_sha == required_change
                    ):
                        reason = (
                            "The repair attempt produced no material patch change. "
                            f"Patch {candidate_patch.patch_sha} is identical to the "
                            "candidate that failed acceptance. Original "
                            f"{last_repair_trigger['stage'] or 'candidate'} failure: "
                            f"{last_repair_trigger['reason'] or 'unknown failure'}"
                        )
                        repair = start_repair(
                            "no_progress",
                            reason,
                            prior_patch_sha=candidate_patch.patch_sha,
                        )
                        if repair is not None:
                            current_step, current_result, required_change = repair
                            continue
                        return current_step, current_result, candidate_patch, {
                            "stage": "no_progress",
                            "reason": reason,
                            "repairable": True,
                        }

                    required_change = ""
                    gates = evaluate_acceptance_gates(
                        step, candidate_patch, worktree_path
                    )
                    if self.runtime:
                        self.runtime.record_event(
                            run_id,
                            "deterministic_gates",
                            step_id=step.step_id,
                            payload={
                                "patch_sha": candidate_patch.patch_sha,
                                "repair_attempts": execution.repair_attempts,
                                **gates.as_dict(),
                            },
                        )
                    if not gates.passed:
                        reason = (
                            "Deterministic acceptance gates failed: "
                            + " ".join(gates.violations)
                        )
                        repair = (
                            start_repair(
                                "acceptance",
                                reason,
                                prior_patch_sha=candidate_patch.patch_sha,
                            )
                            if gates.repairable
                            else None
                        )
                        if repair is not None:
                            current_step, current_result, required_change = repair
                            continue
                        return current_step, current_result, candidate_patch, {
                            "stage": "acceptance",
                            "reason": reason,
                            "repairable": gates.repairable,
                        }

                    return current_step, current_result, candidate_patch, None

            # The dialogue can improve the first draft, but every result still
            # enters the same authoritative candidate loop afterward.
            def consume_dialogue_retry(
                stage: str,
                reason: str,
                turn_result: WorkerResult,
            ) -> bool:
                evidence = getattr(turn_result, "evidence", None) or {}
                return consume_repair(
                    stage,
                    reason,
                    prior_patch_sha=str(evidence.get("patch_sha", "") or ""),
                ) is not None

            candidate_step = step
            if record_metadata.get("retried"):
                retained_paths = [
                    str(item)
                    for item in (
                        record_metadata.get("retained_worktrees", []) or []
                    )
                    if item
                ]
                retry_context = (
                    "This is an explicit operator retry in the retained worktree. "
                    "Preserve useful draft changes, diagnose the prior failure, "
                    "and make the smallest complete correction."
                )
                if retained_paths:
                    retry_context += (
                        " Earlier reviewed drafts remain available at: "
                        + ", ".join(retained_paths)
                    )
                candidate_step = self._revision_step(step, retry_context)
            resume_required_change = ""
            resumable_repair_states = {
                "scheduled",
                "executing",
                "candidate_ready",
                "validating",
                "integration_rollback_pending",
                "active",  # compatibility with older in-flight state
            }
            resuming_repair = (
                persisted_repairs > 0
                and repair_phase["state"] in resumable_repair_states
            )
            if resuming_repair and repair_phase["state"] == "scheduled":
                candidate_step = self._revision_step(
                    step,
                    "Resume the already-budgeted repair attempt in the retained "
                    "worktree. Preserve existing progress and address this failure:\n"
                    + (repair_phase["reason"] or "interrupted repair"),
                )
                result = run_turn(candidate_step)
                resume_required_change = repair_phase["prior_patch_sha"]
            elif resuming_repair:
                # An executing/candidate-ready repair may have been interrupted
                # after mutating files. Re-validate those retained bytes instead
                # of granting another unbudgeted worker turn.
                retained_patch = worktrees.capture_patch(worktree_path, step)
                stored_patch_sha = str(
                    record_metadata.get("current_patch_sha", "") or ""
                )
                if retained_patch.patch_sha != stored_patch_sha:
                    turn_version = max(1, turn_version + 1)
                    self._record_patch_version(
                        run_id,
                        step,
                        retained_patch,
                        turn_version,
                    )
                else:
                    turn_version = max(1, turn_version)
                result = WorkerResult(
                    step_id=step.step_id,
                    raw_response="",
                    result_text="Resuming retained repair candidate after interruption.",
                    files_written=retained_patch.changed_files,
                    evidence=self._turn_evidence(
                        retained_patch,
                        turn_version,
                        step=step,
                        reported_files=retained_patch.changed_files,
                        work_dir=worktree_path,
                    ),
                    success=True,
                )
                candidate_step = self._revision_step(
                    step,
                    "Re-validate the retained repair candidate after interruption. "
                    + (repair_phase["reason"] or ""),
                )
                if repair_phase["stage"] not in {
                    "verification_timeout",
                    "integration_commit",
                }:
                    resume_required_change = repair_phase["prior_patch_sha"]
                self._post_step(
                    step_room,
                    brain_name,
                    "brain",
                    "Resuming retained repair bytes without spending or granting "
                    "another worker turn.",
                    "status",
                )
            elif (
                getattr(self.config, "dialogue", None)
                and self.config.dialogue.enabled
            ):
                outcome = self._run_worker_dialogue(
                    candidate_step,
                    run_turn,
                    worker_state["name"],
                    step_room,
                    output_callback,
                    before_retry=consume_dialogue_retry,
                )
                result = outcome.result
                candidate_step = outcome.last_step or step
                if repair_phase["stage"].startswith("dialogue_"):
                    resume_required_change = repair_phase["prior_patch_sha"]
            else:
                result = run_turn(candidate_step)
                self._post_step(
                    step_room,
                    worker_state["name"],
                    "worker",
                    "Draft patch captured: "
                    f"{len(getattr(result, 'files_written', []) or [])} file(s) "
                    "changed (not yet accepted)",
                    "tool",
                )

            execution.worker_name = worker_state["name"]
            candidate_step, result, patch, failure = prepare_candidate(
                candidate_step,
                result,
                must_change_from=resume_required_change,
            )
            if failure is not None:
                return terminal_failure(
                    str(failure["stage"]),
                    str(failure["reason"]),
                    repairable=bool(failure["repairable"]),
                    block_kind_hint=str(failure.get("block_kind", "") or ""),
                )
            assert result is not None and patch is not None

            verifier = Verifier(
                self.config,
                self.policy or ExecutionPolicy.load(self.work_dir, self.config),
                str(worktree_path),
                output_callback=output_callback,
            )

            # Every repaired candidate re-enters review and verification. No
            # earlier verdict or verification result can authorize new bytes.
            while True:
                while True:
                    review = None
                    review_error = ""
                    for review_attempt in range(2):
                        try:
                            candidate_review = self.review(
                                candidate_step,
                                result,
                                plan=plan,
                                run_id=run_id,
                                work_dir=worktree_path,
                                diff_text=(
                                    patch.patch_text
                                    or "No git diff available."
                                ),
                            )
                            if candidate_review.step_id != step.step_id:
                                raise ValueError(
                                    "Reviewer returned a verdict for "
                                    f"{candidate_review.step_id!r}; expected {step.step_id!r}."
                                )
                            review = candidate_review
                            break
                        except Exception as exc:
                            review_error = str(exc)
                            if review_attempt == 0:
                                status = (
                                    f"Independent review failed for {step.step_id}; "
                                    "retrying the same immutable patch once."
                                )
                                if output_callback:
                                    output_callback(status)
                                self._post_step(
                                    step_room,
                                    execution.reviewer_name,
                                    "reviewer",
                                    status,
                                    "status",
                                )
                                if self.runtime:
                                    self.runtime.record_event(
                                        run_id,
                                        "review_retried",
                                        step_id=step.step_id,
                                        payload={
                                            "patch_sha": patch.patch_sha,
                                            "reason": review_error,
                                            "attempt": 2,
                                        },
                                    )
                    if review is None:
                        return terminal_failure(
                            "review_transport",
                            "Independent reviewer failed twice on the same patch: "
                            + (review_error or "unknown reviewer error"),
                            repairable=False,
                            agent=execution.reviewer_name,
                        )

                    execution.review = review
                    execution.reviewed_patch_sha = patch.patch_sha
                    self._post_step(
                        step_room,
                        execution.reviewer_name,
                        "reviewer",
                        f"[patch v{execution.patch_version} {patch.patch_sha}] "
                        f"{review.verdict} ({review.quality_score}/10): "
                        f"{review.feedback}",
                        "decision",
                    )
                    if self.runtime:
                        self.runtime.upsert_step(
                            run_id,
                            step.step_id,
                            title=step.title,
                            status="reviewing",
                            patch_artifact_id=execution.patch_id,
                            review=review.model_dump(),
                            metadata={
                                "changed_files": patch.changed_files,
                                "reviewed_patch_sha": patch.patch_sha,
                                "review_patch_version": execution.patch_version,
                                "review_state": "current",
                                "repair_attempts": execution.repair_attempts,
                            },
                        )
                    self._record_review(
                        run_id,
                        step,
                        review,
                        reviewer=execution.reviewer_name,
                        repair_attempts=execution.repair_attempts,
                        patch=patch,
                        patch_version=execution.patch_version,
                    )

                    if review.verdict == "approved":
                        break

                    reason = (
                        review.suggested_revision
                        or review.feedback
                        or f"Independent review returned {review.verdict}."
                    )
                    retryable_review = (
                        review.verdict == "needs_revision"
                        and review.should_retry
                    )
                    repair = (
                        start_repair(
                            "review",
                            reason,
                            prior_patch_sha=patch.patch_sha,
                        )
                        if retryable_review
                        else None
                    )
                    if repair is None:
                        return terminal_failure(
                            "review",
                            reason,
                            repairable=retryable_review,
                            agent=execution.reviewer_name,
                        )

                    candidate_step, result, required_change = repair
                    candidate_step, result, patch, failure = prepare_candidate(
                        candidate_step,
                        result,
                        must_change_from=required_change,
                    )
                    if failure is not None:
                        return terminal_failure(
                            str(failure["stage"]),
                            str(failure["reason"]),
                            repairable=bool(failure["repairable"]),
                            block_kind_hint=str(failure.get("block_kind", "") or ""),
                        )
                    assert result is not None and patch is not None

                if self.runtime:
                    self.runtime.upsert_step(
                        run_id,
                        step.step_id,
                        title=step.title,
                        status="verifying",
                        metadata={"repair_attempts": execution.repair_attempts},
                    )
                verification = verifier.verify(changed_files=patch.changed_files)
                execution.verification = verification
                self._record_verification(
                    run_id,
                    step.step_id,
                    verification,
                    verified_patch_sha=patch.patch_sha,
                )
                if self.runtime:
                    self.runtime.upsert_step(
                        run_id,
                        step.step_id,
                        title=step.title,
                        status="verifying",
                        verification=self._verification_payload(
                            verification,
                            verified_patch_sha=patch.patch_sha,
                        ),
                        metadata={
                            "repair_attempts": execution.repair_attempts,
                            "verified_patch_sha": patch.patch_sha,
                        },
                    )

                # Verification commands are untrusted with respect to the
                # worktree: formatters and test scripts can modify files. Never
                # commit the pre-verification patch if the verified bytes differ.
                verified_patch = worktrees.capture_patch(worktree_path, step)
                if verified_patch.patch_sha != patch.patch_sha:
                    reviewed_sha = patch.patch_sha
                    reason = (
                        "Verification modified the candidate patch after review: "
                        f"reviewed={reviewed_sha}, verified={verified_patch.patch_sha}. "
                        "The changed bytes require fresh gates, review, and verification."
                    )
                    mutation_repair = consume_repair(
                        "verification_mutation",
                        reason,
                        prior_patch_sha=reviewed_sha,
                        worker_turn=False,
                    )
                    turn_version += 1
                    result.files_written = verified_patch.changed_files
                    result.evidence = self._turn_evidence(
                        verified_patch,
                        turn_version,
                        step=step,
                        reported_files=verified_patch.changed_files,
                        work_dir=worktree_path,
                    )
                    self._record_patch_version(
                        run_id,
                        step,
                        verified_patch,
                        turn_version,
                    )

                    if mutation_repair is None:
                        execution.result = result
                        execution.patch = verified_patch
                        execution.patch_version = turn_version
                        execution.patch_id = self._store_patch(
                            run_id,
                            step.step_id,
                            verified_patch,
                        )
                        execution.review = None
                        execution.reviewed_patch_sha = ""
                        if self.runtime:
                            self.runtime.upsert_step(
                                run_id,
                                step.step_id,
                                title=step.title,
                                status="reviewing",
                                patch_artifact_id=execution.patch_id,
                                review={},
                                metadata={
                                    "changed_files": verified_patch.changed_files,
                                    "review_state": "unreviewed",
                                    "repair_attempts": execution.repair_attempts,
                                },
                            )
                        return terminal_failure(
                            "verification_mutation",
                            reason,
                            repairable=True,
                            agent="verifier",
                        )

                    candidate_step = self._revision_step(
                        step,
                        "Verification changed the worktree. Treat the resulting "
                        "bytes as a new candidate and independently validate them.",
                    )
                    candidate_step, result, patch, failure = prepare_candidate(
                        candidate_step,
                        result,
                        must_change_from=reviewed_sha,
                    )
                    if failure is not None:
                        return terminal_failure(
                            str(failure["stage"]),
                            str(failure["reason"]),
                            repairable=bool(failure["repairable"]),
                            block_kind_hint=str(failure.get("block_kind", "") or ""),
                        )
                    assert result is not None and patch is not None
                    continue

                if verification.passed:
                    stage_repair_outcome(patch)
                    return execution

                reason = self._verification_summary(verification)
                if verification.failure_kind == "timeout":
                    timeout_retry = consume_repair(
                        "verification_timeout",
                        reason,
                        prior_patch_sha=patch.patch_sha,
                        invalidate_candidate=False,
                        worker_turn=False,
                    )
                    if timeout_retry is not None:
                        # The candidate bytes did not fail; retry the operational
                        # check without forcing a meaningless source change.
                        continue
                    if not self.config.verification.require_for_commit:
                        if output_callback:
                            output_callback(
                                "Verification timed out and no retry budget remains, "
                                "but require_for_commit=false; committing advisory result."
                            )
                        stage_repair_outcome(patch, "accepted_advisory")
                        return execution
                    return terminal_failure(
                        "verification_timeout",
                        reason,
                        repairable=True,
                        agent="verifier",
                    )

                if not verification.repairable:
                    return terminal_failure(
                        "verification",
                        reason,
                        repairable=False,
                        agent="verifier",
                    )

                repair = start_repair(
                    "verification",
                    reason,
                    prior_patch_sha=patch.patch_sha,
                )
                if repair is None:
                    if not self.config.verification.require_for_commit:
                        if output_callback:
                            output_callback(
                                "Verification failed and automatic repair is "
                                "unavailable, but require_for_commit=false; "
                                "committing advisory result: "
                                f"{verification.reason}"
                            )
                        stage_repair_outcome(patch, "accepted_advisory")
                        return execution
                    return terminal_failure(
                        "verification",
                        reason,
                        repairable=True,
                        agent="verifier",
                    )

                candidate_step, result, required_change = repair
                candidate_step, result, patch, failure = prepare_candidate(
                    candidate_step,
                    result,
                    must_change_from=required_change,
                )
                if failure is not None:
                    return terminal_failure(
                        str(failure["stage"]),
                        str(failure["reason"]),
                        repairable=bool(failure["repairable"]),
                        block_kind_hint=str(failure.get("block_kind", "") or ""),
                    )
                assert result is not None and patch is not None
        except Exception as e:
            logger.exception("Step execution failed for %s", step.step_id)
            execution.failed_agent = "worker-runtime"
            execution.failed_reason = str(e)
            execution.memory_note = f"Execution failed: {e}"
            return execution

    def _retry_step_ids(self, plan: Plan, retry_step_id: str) -> list[str]:
        dependents: dict[str, set[str]] = {step.step_id: set() for step in plan.steps}
        for step in plan.steps:
            for dep in step.depends_on:
                dependents.setdefault(dep, set()).add(step.step_id)

        selected: list[str] = []
        queue = [retry_step_id]
        seen: set[str] = set()
        while queue:
            step_id = queue.pop(0)
            if step_id in seen:
                continue
            seen.add(step_id)
            selected.append(step_id)
            queue.extend(sorted(dependents.get(step_id, set())))
        return selected

    def _scope_metadata(
        self,
        step: Step,
        scope: StepScope,
        *,
        lease: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        declared = declared_step_scope(step)
        inferred = infer_step_scope(step)
        metadata = {
            "declared_scope": list(declared.paths) if declared else [],
            "inferred_scope": list(inferred.paths),
            "effective_scope": list(scope.paths),
            "scope_source": scope.source,
            "scope": list(scope.paths),
        }
        if lease is not None:
            metadata["lease"] = lease
        metadata.update(extra or {})
        return metadata

    def _reviewer_name(self) -> str:
        return getattr(self._review_agent(), "name", "independent-reviewer") or "independent-reviewer"

    # ── Per-step worker rooms (observable in the viewer) ─────────────────────

    def _open_step_room(self, step: Step, worker_name: str, specialty: str) -> str:
        """Open a chatroom for this step where the brain briefs the worker; the
        worker's result and the reviewer's verdict are posted here too. Returns
        the room id, or '' when there is no chatroom. Never raises."""
        if self.chatroom is None:
            return ""
        try:
            from genesis.chatroom import RoomKind

            brain = getattr(self.agent, "name", "brain") or "brain"
            room = self.chatroom.create_room(
                RoomKind.worker_room,
                f"{step.step_id} — {step.title}",
                participants=[brain, worker_name],
            )
            self.chatroom.post(
                room.id, brain, "brain",
                f"Brief for {worker_name} · specialty: {specialty}\n"
                f"{step.description}\nExpected: {step.expected_output}",
            )
            return room.id
        except Exception:
            return ""

    def _post_step(self, room_id: str, sender: str, role: str, content: str,
                   kind: str = "message") -> None:
        if self.chatroom is None or not room_id:
            return
        try:
            self.chatroom.post(room_id, sender, role, content, kind)
        except Exception:
            pass

    # ── Multi-turn brain<->worker dialogue ───────────────────────────────────

    def _brain_evaluate(self, step: Step, result: WorkerResult) -> tuple[bool, str]:
        """The directing brain judges a worker turn: approve, or revise with
        specific feedback. Parse failures fail open (approve) so a flaky judge
        can never trap the dialogue in a loop."""
        director_system = (
            "You are the brain directing a worker on a single step. Given the worker's latest "
            "result, decide whether the implementation is complete and correct — including any "
            "tests the step needs — or requires another revision. Be strict about tests and "
            "correctness, but do not ask for gold-plating.\n"
            'Return ONLY JSON: {"action":"approve"|"revise","feedback":"<specific, actionable '
            'changes if revise; empty if approve>"}'
        )
        files = result.files_written or []
        body = (result.result_text or "")[:3000]
        evidence = getattr(result, "evidence", None) or {}
        patch_text = str(evidence.get("patch_text", ""))
        patch_block = self._bounded_diff(patch_text, max_chars=8000) if patch_text else "No patch captured."
        status_lines = evidence.get("status_lines", [])
        status_block = "\n".join(str(line) for line in status_lines) or "clean"
        diff_status_lines = evidence.get("diff_status_lines", [])
        diff_status_block = (
            "\n".join(str(line) for line in diff_status_lines)
            or "no base-relative changes"
        )
        msg = (
            f"STEP:\n  ID: {step.step_id}\n  Title: {step.title}\n  Type: {step.type}\n"
            f"  Expected Output: {step.expected_output}\n\n"
            f"AUTHORITATIVE TURN EVIDENCE:\n"
            f"  Version: {evidence.get('version', 'unknown')}\n"
            f"  Patch ID: {evidence.get('patch_sha', 'none')}\n"
            f"  Base: {evidence.get('base_sha', 'unknown')}\n"
            f"  Head: {evidence.get('head_sha', 'unknown')}\n"
            f"  Changed Files ({len(files)}): {', '.join(files) or 'none'}\n"
            f"  Working Tree Status:\n{status_block}\n"
            f"  Base-relative Diff Status:\n{diff_status_block}\n\n"
            f"ACTUAL PATCH:\n{patch_block}\n\n"
            f"WORKER SUMMARY:\n{body}\n\n"
            'Return ONLY the JSON object.'
        )
        try:
            raw = self._invoke(
                self._brain_candidates(self.agent),
                lambda a: a.chat(director_system, [{"role": "user", "content": msg}]),
            )
            data = self._extract_json(raw)
        except Exception as e:
            logger.warning("Director evaluation failed (%s); approving to avoid a loop", e)
            return True, ""
        action = str(data.get("action", "approve")).strip().lower()
        feedback = str(data.get("feedback", "")).strip()
        return (action != "revise"), feedback

    @staticmethod
    def _turn_evidence(
        patch: WorktreePatch,
        version: int,
        *,
        step: Step | None = None,
        reported_files: list[str] | None = None,
        work_dir: str | Path | None = None,
    ) -> dict:
        guard = evaluate_patch_evidence(step, patch) if step is not None else None
        gates = (
            evaluate_acceptance_gates(
                step,
                patch,
                work_dir,
                run_external_scanners=False,
            )
            if step is not None and work_dir is not None
            else None
        )
        return {
            "version": version,
            "patch_sha": patch.patch_sha,
            "base_sha": patch.base_sha,
            "head_sha": patch.head_sha,
            "changed_files": list(patch.changed_files),
            "turn_reported_files": list(reported_files or []),
            "status_lines": list(patch.status_lines),
            "diff_status_lines": list(patch.diff_status_lines),
            "patch_text": patch.patch_text,
            "guard_violations": (
                list(gates.violations)
                if gates is not None
                else list(guard.violations) if guard else []
            ),
            "acceptance_gates": gates.as_dict() if gates is not None else {},
        }

    def _run_worker_dialogue(self, step: Step, run_worker, worker_name: str,
                             step_room: str, output_callback,
                             before_retry=None):
        from genesis.agents.worker_dialogue import WorkerDialogue

        brain = getattr(self.agent, "name", "brain") or "brain"
        dialogue = WorkerDialogue(
            step=step,
            worker_name=worker_name,
            brain_name=brain,
            max_turns=self.config.dialogue.max_turns,
            run_worker=run_worker,
            evaluate=lambda s, r, t: self._brain_evaluate(s, r),
            make_revision=self._revision_step,
            post=lambda sender, role, content, kind="message": self._post_step(
                step_room, sender, role, content, kind),
            on_status=output_callback,
            fast_path=getattr(self.config.dialogue, "fast_path", True),
            before_retry=before_retry,
        )
        outcome = dialogue.run()
        if output_callback:
            output_callback(
                f"Dialogue on {step.step_id}: {outcome.turns} turn(s), "
                f"{'brain-approved' if outcome.approved else 'sent to review'}"
            )
        return outcome

    _SPECIALTIES = {
        "test": "testing & test coverage",
        "code": "implementation",
        "docs": "documentation",
        "review": "code review",
        "research": "research & investigation",
        "config": "configuration & tooling",
        "refactor": "refactoring & cleanup",
    }

    def _specialty_for(self, step: Step) -> str:
        return self._SPECIALTIES.get((step.type or "").lower(), "implementation")

    def _run_context(
        self,
        plan: Plan | None,
        run_id: str,
        current_step: Step | None = None,
        max_chars: int = 12000,
    ) -> str:
        if plan is None:
            return "No retained plan context is available."

        original_task = plan.task_summary
        records = {}
        if self.runtime and run_id:
            run = self.runtime.get_run(run_id)
            if run:
                original_task = run.task
            records = {record.step_id: record for record in self.runtime.steps(run_id)}
            workspace = self.runtime.get_checkpoint(run_id, "workspace_preflight")
        else:
            workspace = None

        lines = [
            f"Original task: {original_task}",
            f"Plan summary: {plan.task_summary}",
            "Retained plan and current state:",
        ]
        if workspace:
            lines.extend([
                "Workspace preflight:",
                f"- HEAD: {workspace.get('head') or 'unborn'}; "
                f"tracked files: {workspace.get('tracked_count', 0)}",
                "- Dirty: " + (", ".join(workspace.get("dirty", [])[:20]) or "none"),
                "- Untracked: " + (", ".join(workspace.get("untracked", [])[:20]) or "none"),
                "- Ignored source available for task overlay: "
                + (", ".join(workspace.get("ignored_source", [])[:20]) or "none"),
                "- Credential-sensitive paths excluded from checkpoints: "
                + (", ".join(workspace.get("sensitive_paths", [])[:20]) or "none"),
            ])
        for planned_step in plan.steps:
            record = records.get(planned_step.step_id)
            status = record.status if record else "pending"
            marker = " (current)" if current_step and planned_step.step_id == current_step.step_id else ""
            lines.append(
                f"- {planned_step.step_id} [{status}]{marker}: {planned_step.title}; "
                f"depends on {', '.join(planned_step.depends_on) or 'none'}; "
                f"success: {planned_step.expected_output}"
            )
            description = " ".join(planned_step.description.split())[:800]
            if description:
                lines.append(f"  Brief: {description}")
            if record and record.review_json.get("memory_note"):
                lines.append(f"  Result: {record.review_json['memory_note']}")
            changed = record.metadata.get("changed_files", []) if record else []
            if changed:
                lines.append(f"  Files: {', '.join(str(path) for path in changed)}")

        context = "\n".join(lines)
        if len(context) > max_chars:
            context = context[:max_chars].rstrip() + "\n... (run context truncated)"
        return context

    def _step_memory(
        self,
        step: Step,
        plan: Plan | None = None,
        run_id: str = "",
    ) -> str:
        memory_budget = max(0, int(self.config.memory.max_context_chars))
        try:
            markdown_context = self.memory.get_summary(memory_budget)
        except Exception as exc:
            logger.warning("Markdown memory wakeup failed for %s: %s", step.step_id, exc)
            markdown_context = ""
        palace_context = ""
        if memory_budget and self.palace and self.config.memory.palace_enabled:
            try:
                palace_context = self.palace.wakeup_context(
                    f"{step.title}\n{step.description}",
                    max_chars=memory_budget,
                    wing=str(Path(self.work_dir).resolve()),
                )
            except Exception as exc:
                logger.warning("Palace wakeup failed for %s: %s", step.step_id, exc)
        markdown_context, palace_context = self._fit_memory_sections(
            markdown_context,
            palace_context,
            memory_budget,
        )
        mem_summary = markdown_context
        if palace_context:
            separator = "\n\n---\n\n" if mem_summary else ""
            mem_summary += separator + palace_context
        # Phase 3: give the worker a specialty framing so it focuses on producing
        # fully-tested, production-quality work in the area this step needs.
        specialty = self._specialty_for(step)
        directive = (
            f"YOUR SPECIALTY FOR THIS STEP: {specialty}. "
            f"Produce complete, fully-tested, production-quality work in this area."
        )
        run_context = self._run_context(plan, run_id, step)
        return (
            f"{directive}\n\n---\n\n"
            f"SHARED RETAINED RUN CONTEXT:\n{run_context}\n\n---\n\n"
            f"PROJECT MEMORY:\n{mem_summary}"
        )

    def _store_patch(self, run_id: str, step_id: str, patch: WorktreePatch) -> str:
        if not self.runtime:
            return ""
        return self.runtime.add_artifact(
            run_id,
            step_id=step_id,
            kind="patch",
            path="",
            content=patch.patch_text,
            metadata={
                "worktree_path": patch.worktree_path,
                "changed_files": patch.changed_files,
                "base_sha": patch.base_sha,
                "head_sha": patch.head_sha,
                "patch_sha": patch.patch_sha,
            },
        )

    def _remember_step(
        self,
        run_id: str,
        step: Step,
        worker_name: str,
        review: Review,
        patch: WorktreePatch,
        *,
        patch_artifact_id: str = "",
        commit_sha: str = "",
    ) -> None:
        self._palace_add(
            run_id=run_id,
            step_id=step.step_id,
            closet="steps",
            kind="step-result",
            title=f"{step.step_id}: {step.title}",
            content=(
                f"Worker: {worker_name}\n"
                f"Verdict: {review.verdict}\n"
                f"Score: {review.quality_score}/10\n\n"
                f"Memory note:\n{review.memory_note}\n\n"
                f"Files:\n{chr(10).join(patch.changed_files)}\n\n"
                f"Commit: {commit_sha or 'not recorded'}\n"
                f"Patch SHA: {patch.patch_sha}\n"
                f"Patch artifact: {patch_artifact_id or 'not retained'}"
            ),
            status=review.verdict,
            metadata={
                "worker": worker_name,
                "files": patch.changed_files,
                "commit_sha": commit_sha,
                "patch_sha": patch.patch_sha,
                "patch_artifact_id": patch_artifact_id,
            },
        )

    def _record_review(
        self,
        run_id: str,
        step: Step,
        review: Review,
        *,
        reviewer: str,
        repair_attempts: int,
        patch: WorktreePatch,
        patch_version: int,
    ) -> None:
        if not self.runtime:
            return
        payload = {
            "reviewer": reviewer,
            "verdict": review.verdict,
            "quality_score": review.quality_score,
            "feedback": review.feedback,
            "should_retry": review.should_retry,
            "suggested_revision": review.suggested_revision,
            "repair_attempts": repair_attempts,
            "patch_sha": patch.patch_sha,
            "patch_version": patch_version,
        }
        self.runtime.record_event(
            run_id,
            "review_completed",
            step_id=step.step_id,
            payload=payload,
        )
        self.runtime.upsert_step(
            run_id,
            step.step_id,
            title=step.title,
            metadata={
                "reviewer": reviewer,
                "review_verdict": review.verdict,
                "current_patch_sha": patch.patch_sha,
                "current_patch_version": patch_version,
                "reviewed_patch_sha": patch.patch_sha,
                "review_patch_version": patch_version,
                "review_state": "current",
                "repair_attempts": repair_attempts,
            },
        )

    def _record_patch_version(
        self,
        run_id: str,
        step: Step,
        patch: WorktreePatch,
        version: int,
    ) -> None:
        if not self.runtime:
            return
        current = self.runtime.get_step(run_id, step.step_id)
        metadata = current.metadata if current else {}
        reviewed_sha = str(metadata.get("reviewed_patch_sha", "") or "")
        reviewed_version = metadata.get("review_patch_version", 0)
        if reviewed_sha and reviewed_sha != patch.patch_sha:
            self.runtime.record_event(
                run_id,
                "review_superseded",
                step_id=step.step_id,
                payload={
                    "reviewed_patch_sha": reviewed_sha,
                    "review_patch_version": reviewed_version,
                    "replacement_patch_sha": patch.patch_sha,
                    "replacement_patch_version": version,
                },
            )
        self.runtime.record_event(
            run_id,
            "patch_version_captured",
            step_id=step.step_id,
            payload={
                "patch_sha": patch.patch_sha,
                "patch_version": version,
                "base_sha": patch.base_sha,
                "head_sha": patch.head_sha,
                "changed_files": patch.changed_files,
            },
        )
        self.runtime.upsert_step(
            run_id,
            step.step_id,
            title=step.title,
            review={},
            verification={},
            metadata={
                "current_patch_sha": patch.patch_sha,
                "current_patch_version": version,
                "review_state": "unreviewed",
                "review_verdict": "",
                "verified_patch_sha": "",
            },
        )

    def _cleanup_step_worktrees(
        self,
        run_id: str,
        step: Step,
        paths: list[str],
        worktrees: WorktreeManager,
    ) -> list[str]:
        """Idempotently finish worktree cleanup recorded with a commit."""
        remaining: list[str] = []
        cleaned: list[str] = []
        for cleanup_path in dict.fromkeys(str(item) for item in paths if item):
            try:
                worktrees.remove(cleanup_path)
                cleaned.append(cleanup_path)
            except Exception as exc:
                remaining.append(cleanup_path)
                logger.warning(
                    "Could not remove worktree %s: %s",
                    cleanup_path,
                    exc,
                )
        if self.runtime:
            try:
                self.runtime.record_step_event(
                    run_id,
                    step.step_id,
                    (
                        "worktree_cleanup_completed"
                        if not remaining
                        else "worktree_cleanup_deferred"
                    ),
                    title=step.title,
                    status="committed",
                    worktree_path="" if not remaining else None,
                    metadata={
                        "cleanup_pending_worktrees": remaining,
                    },
                    payload={
                        "cleaned": cleaned,
                        "remaining": remaining,
                    },
                )
            except Exception:
                # The commit transition still contains the original pending
                # list, making physical cleanup safe to retry after a crash.
                logger.exception(
                    "Could not persist worktree cleanup for %s", step.step_id
                )
        return remaining

    def _schedule_integration_repair(
        self,
        run_id: str,
        step: Step,
        execution: _StepExecution,
        *,
        stage: str,
        reason: str,
        worktrees: WorktreeManager,
        worktree_lock,
        fire: Callable,
        refresh_base: bool,
        rollback_pending: bool = False,
    ) -> bool:
        """Consume one persisted repair turn and safely re-lease the step.

        Apply conflicts get a fresh worktree based on current committed main;
        the prior reviewed draft remains available for the mentor worker to
        inspect. Commit failures reuse the immutable retained candidate after
        rollback, so an operational retry cannot invent unreviewed bytes.
        """
        if not self.runtime or not execution.patch:
            return False

        budget = max(0, int(self.config.runtime.retry_budget))
        record = self.runtime.get_step(run_id, step.step_id)
        if not record:
            return False
        try:
            persisted = max(
                0, int(record.metadata.get("repair_attempts", 0) or 0)
            )
        except (TypeError, ValueError):
            persisted = 0
        attempts_used = max(execution.repair_attempts, persisted)
        if attempts_used >= budget:
            return False

        attempt = attempts_used + 1
        attempts_left = max(0, budget - attempt)
        prior_path = Path(record.worktree_path or execution.worktree_path or "")
        next_path = prior_path
        retained = [
            str(item)
            for item in (record.metadata.get("retained_worktrees", []) or [])
            if item
        ]
        created_path: Path | None = None

        if refresh_base:
            try:
                with worktree_lock:
                    created_path = worktrees.create(
                        run_id,
                        f"{step.step_id}-integration-{attempt}-{uuid.uuid4().hex[:6]}",
                    )
                    worktrees.materialize_referenced_ignored(created_path, step)
                next_path = created_path
                if prior_path and str(prior_path) not in retained:
                    retained.append(str(prior_path))
            except Exception as exc:
                logger.warning(
                    "Could not prepare fresh integration repair worktree for %s: %s",
                    step.step_id,
                    exc,
                )
                if created_path:
                    try:
                        worktrees.remove(created_path)
                    except Exception:
                        pass
                return False

        detailed_reason = str(reason or "integration failed").strip()
        if refresh_base:
            detailed_reason += (
                " The reviewed draft remains at "
                f"{prior_path}. Continue in the fresh worktree based on current "
                f"main at {next_path}; inspect the retained draft, preserve its "
                "correct intent, and reconcile only the requested changes."
            )
        elif rollback_pending:
            detailed_reason += (
                " The approved paths are being restored exactly to HEAD before "
                "the immutable retained candidate is revalidated."
            )
        else:
            detailed_reason += (
                " Main was restored exactly to HEAD. Re-run the immutable retained "
                "candidate through gates, review, verification, and commit."
            )

        repair_state = (
            "scheduled"
            if refresh_base
            else "integration_rollback_pending"
            if rollback_pending
            else "validating"
        )
        try:
            event = self._record_repair_attempt(
                run_id,
                step,
                reason=detailed_reason,
                attempts_used=attempt,
                attempts_left=attempts_left,
                stage=stage,
                budget_total=budget,
                prior_patch_sha=execution.patch.patch_sha,
                worker=execution.worker_name,
                repair_state=repair_state,
                worktree_path=str(next_path),
                extra_metadata={
                    "retained_worktrees": retained,
                    "integration_retry_from": str(prior_path),
                    "draft_retained": True,
                },
            )
        except Exception:
            logger.exception(
                "Could not persist integration repair for %s", step.step_id
            )
            if created_path:
                try:
                    worktrees.remove(created_path)
                except Exception:
                    pass
            return False

        execution.repair_attempts = attempt
        execution.repair_id = str(event.get("repair_id", "") or "")
        execution.worktree_path = next_path
        fire("on_repair", step, event)
        fire(
            "on_status",
            f"REPAIR {attempt}/{budget} | {step.step_id} | {stage}\n"
            f"{detailed_reason}",
        )
        return True

    def _record_repair_attempt(
        self,
        run_id: str,
        step: Step,
        *,
        reason: str,
        attempts_used: int,
        attempts_left: int,
        stage: str = "review",
        budget_total: int | None = None,
        prior_patch_sha: str = "",
        worker: str = "",
        repair_state: str = "scheduled",
        worktree_path: str | None = None,
        extra_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        total = (
            max(0, int(budget_total))
            if budget_total is not None
            else attempts_used + attempts_left
        )
        repair_id = uuid.uuid4().hex[:12]
        payload: dict[str, object] = {
            "schema_version": 2,
            "repair_id": repair_id,
            "stage": stage,
            "reason": reason,
            "attempts_used": attempts_used,
            "attempts_left": attempts_left,
            "budget_total": total,
            "prior_patch_sha": prior_patch_sha,
            "worker": worker,
            "repair_state": repair_state,
        }
        if not self.runtime:
            return payload
        metadata: dict[str, object] = {
            "repair_attempts": attempts_used,
            "repair_budget": total,
            "repairs_remaining": attempts_left,
            "repair_state": repair_state,
            "repair_stage": stage,
            "repair_id": repair_id,
            "repair_prior_patch_sha": prior_patch_sha,
            "last_repair_reason": reason,
        }
        metadata.update(extra_metadata or {})
        self.runtime.record_step_event(
            run_id,
            step.step_id,
            "repair_attempted",
            title=step.title,
            status="repairing",
            worktree_path=worktree_path,
            metadata=metadata,
            payload=payload,
        )
        return payload

    def _repair_outcome_details(
        self,
        step: Step,
        execution: _StepExecution,
        *,
        outcome: str = "",
    ) -> tuple[str, dict[str, object], dict[str, object], str]:
        if execution.repair_attempts <= 0:
            return "", {}, {}, ""
        final_outcome = outcome or execution.repair_outcome
        if not final_outcome:
            return "", {}, {}, ""

        if final_outcome == "verified":
            event_type = "repair_resolved"
            repair_state = "resolved"
            message = (
                f"REPAIR RESOLVED | {step.step_id} | "
                f"committed patch {execution.repair_patch_sha or 'unknown'}"
            )
        elif final_outcome == "accepted_advisory":
            event_type = "repair_advisory_accepted"
            repair_state = "accepted_advisory"
            message = (
                f"REPAIR CLOSED AS ADVISORY | {step.step_id} | "
                "verification still reports a failure"
            )
        else:
            event_type = "repair_integration_failed"
            repair_state = "integration_failed"
            message = (
                f"REPAIR NOT INTEGRATED | {step.step_id} | "
                "reviewed draft retained for operator retry"
            )

        budget_total = max(0, int(self.config.runtime.retry_budget))
        payload: dict[str, object] = {
            "repair_id": execution.repair_id,
            "outcome": final_outcome,
            "attempts_used": execution.repair_attempts,
            "budget_total": budget_total,
            "patch_sha": execution.repair_patch_sha,
        }
        if execution.verification is not None:
            payload["verification"] = self._verification_payload(
                execution.verification
            )
        metadata: dict[str, object] = {
            "repair_state": repair_state,
            "repair_outcome": final_outcome,
            "repair_stage": "",
            "repair_id": "",
            "repair_final_patch_sha": execution.repair_patch_sha,
            "repairs_remaining": max(
                0, budget_total - execution.repair_attempts
            ),
        }
        if final_outcome == "verified":
            metadata["last_repair_reason"] = ""
        return event_type, metadata, payload, message

    def _record_repair_outcome(
        self,
        run_id: str,
        step: Step,
        execution: _StepExecution,
        *,
        outcome: str = "",
    ) -> str:
        """Close a non-commit repair outcome as one durable transition."""
        event_type, metadata, payload, message = self._repair_outcome_details(
            step,
            execution,
            outcome=outcome,
        )
        if event_type and self.runtime:
            self.runtime.record_step_event(
                run_id,
                step.step_id,
                event_type,
                title=step.title,
                status="blocked" if outcome == "integration_failed" else None,
                metadata=metadata,
                payload=payload,
            )
        return message

    def _revision_step(self, step: Step, feedback: str) -> Step:
        return step.model_copy(update={
            "description": step.description + f"\n\nREVISION REQUIRED: {feedback}"
        })

    @staticmethod
    def _verification_summary(verification: VerificationResult) -> str:
        if verification.commands:
            last = verification.commands[-1]
            output = f"\nCommand: {last.command}\nExit: {last.returncode}\nOutput:\n{last.output[:1200]}"
        else:
            output = ""
        return f"Verification failed: {verification.reason}{output}"

    def _release_summary(self, plan: Plan, completed: int, total: int) -> str:
        lines = [
            f"Task: {plan.task_summary}",
            f"Completed steps: {completed}/{total}",
        ]
        if self.runtime:
            for step in self.runtime.steps(plan.task_id):
                files = step.metadata.get("changed_files", [])
                if isinstance(files, list):
                    files_text = ", ".join(files)
                else:
                    files_text = str(files)
                verifier = step.verification_json or {}
                verification = "skipped" if verifier.get("skipped") else "passed" if verifier.get("passed") else "unknown"
                lines.append(
                    f"- {step.step_id}: {step.status}; files: {files_text or 'none'}; verification: {verification}"
                )
        return "\n".join(lines)

    def _verification_payload(
        self,
        verification: VerificationResult,
        *,
        verified_patch_sha: str = "",
    ) -> dict:
        payload = {
            "passed": verification.passed,
            "skipped": verification.skipped,
            "reason": verification.reason,
            "failure_kind": verification.failure_kind,
            "repairable": verification.repairable,
            "commands": [
                {
                    "command": cmd.command,
                    "returncode": cmd.returncode,
                    "output": cmd.output,
                }
                for cmd in verification.commands
            ],
        }
        if verified_patch_sha:
            payload["verified_patch_sha"] = verified_patch_sha
        return payload

    def _block_step(
        self,
        run_id: str,
        step: Step,
        agent: str,
        memory_note: str,
        reason: str,
        fire: Callable,
    ) -> None:
        fire("on_error", step, reason)
        self._try_memory_write(
            "append_blocked_step",
            lambda: self.memory.append_step(
                step.step_id,
                step.title,
                agent,
                memory_note,
                "rejected",
            ),
            run_id=run_id,
            step_id=step.step_id,
        )
        if self.runtime:
            self.runtime.upsert_step(
                run_id,
                step.step_id,
                title=step.title,
                status="blocked",
                metadata={"blocked_reason": reason, "lease": "blocked"},
            )
            self.runtime.update_run_status(
                run_id,
                "blocked",
                metadata={"blocked_step": step.step_id, "reason": reason},
            )
        self._palace_add(
            run_id=run_id,
            step_id=step.step_id,
            closet="failures",
            kind="blocked-step",
            title=f"{step.step_id}: {step.title}",
            content=reason,
            status="rejected",
            metadata={"agent": agent},
        )

    def _palace_add(
        self,
        *,
        run_id: str,
        step_id: str,
        closet: str,
        kind: str,
        title: str,
        content: str,
        status: str = "",
        metadata: dict | None = None,
    ) -> None:
        if not self.palace or not self.config.memory.palace_enabled:
            return
        try:
            self.palace.add_drawer(
                wing=str(Path(self.work_dir).resolve()),
                room=run_id or "session",
                closet=closet,
                kind=kind,
                title=title,
                content=content,
                source="genesis-runtime",
                run_id=run_id,
                step_id=step_id,
                status=status,
                metadata=metadata,
            )
        except Exception as e:
            logger.warning("Could not write palace memory: %s", e)

    def _record_verification(
        self,
        run_id: str,
        step_id: str,
        verification: VerificationResult,
        *,
        verified_patch_sha: str,
    ) -> None:
        payload = self._verification_payload(
            verification,
            verified_patch_sha=verified_patch_sha,
        )
        if self.runtime:
            self.runtime.record_event(
                run_id,
                "verification_completed",
                step_id=step_id,
                payload=payload,
            )
        self._palace_add(
            run_id=run_id,
            step_id=step_id,
            closet="verification",
            kind="verification-result",
            title=f"Verification for {step_id}",
            content=json.dumps(payload, indent=2, ensure_ascii=False),
            status="approved" if verification.passed else "rejected",
        )

    def _assign_worker(self, step: Step, unavailable: set[str] | None = None) -> tuple[str, BaseAgent]:
        candidates = self._eligible_worker_candidates(unavailable)
        if not candidates:
            raise RuntimeError("No worker agents available")

        # Direct match by exact key name
        if step.preferred_agent not in ("any", "codex-worker", "claude-worker"):
            if step.preferred_agent in candidates:
                return step.preferred_agent, candidates[step.preferred_agent]

        # Always prefer Codex workers — keep Claude for orchestration only
        for name, agent in candidates.items():
            if "claude" not in name.lower():
                return name, agent

        # Fallback: use whatever is available
        name, agent = next(iter(candidates.items()))
        return name, agent

    def _eligible_worker_candidates(
        self,
        unavailable: set[str] | None = None,
    ) -> dict[str, BaseAgent]:
        """Return available workers while keeping reserve accounts dormant.

        A reserve becomes eligible only after every normal account from the
        same provider is marked exhausted. Merely being busy on another step
        does not unlock the reserve, so parallel scheduling cannot consume the
        last-resort account early.
        """
        unavailable = set(unavailable or set())
        exhausted = (
            self.registry.exhausted_names()
            if self._failover_enabled()
            else set()
        )
        blocked = unavailable | exhausted
        candidates = {
            name: agent
            for name, agent in self.worker_agents.items()
            if name not in blocked
        }

        for name, agent in list(candidates.items()):
            if not getattr(agent, "reserve", False):
                continue
            provider = getattr(agent, "provider", "")
            normal_peers = {
                peer_name
                for peer_name, peer in self.worker_agents.items()
                if not getattr(peer, "reserve", False)
                and getattr(peer, "provider", "") == provider
            }
            if normal_peers and not normal_peers.issubset(exhausted):
                candidates.pop(name)
        return candidates

    # ── Account failover ─────────────────────────────────────────────────────

    def _failover_enabled(self) -> bool:
        return getattr(getattr(self.config, "failover", None), "enabled", True)

    def _notify_failover(self, name: str, room_id: str = "", alt: str = "") -> None:
        target = f" — routing to {alt}" if alt else ""
        logger.warning("Account '%s' exhausted%s; failing over", name, target)
        self._post_step(room_id, name, "system",
                        f"{name} hit its rate/usage limit{target}", "status")

    def _notify_timeout_failover(self, name: str, room_id: str, alt: str) -> None:
        logger.warning("Worker '%s' became inactive; routing to %s", name, alt)
        self._post_step(
            room_id,
            name,
            "system",
            f"{name} stopped producing activity — continuing with {alt}",
            "status",
        )

    def _brain_candidates(self, primary: BaseAgent) -> list[BaseAgent]:
        """Ordered, de-duplicated brain-capable agents for a role: the preferred
        agent, the two brains, then any account (each CLI can plan/review)."""
        out: list[BaseAgent] = []
        seen: set[str] = set()
        normal_workers = [
            agent for agent in self.worker_agents.values()
            if not getattr(agent, "reserve", False)
        ]
        reserve_workers = [
            agent for agent in self.worker_agents.values()
            if getattr(agent, "reserve", False)
        ]
        for agent in [
            primary,
            self.agent,
            self.co_brain,
            *normal_workers,
            *reserve_workers,
        ]:
            if agent is None:
                continue
            name = getattr(agent, "name", "")
            if name in seen:
                continue
            seen.add(name)
            out.append(agent)
        return out

    def _invoke(self, candidates: list[BaseAgent], make_call: Callable[[BaseAgent], str]) -> str:
        """Call make_call on the first available agent; on an exhaustion error,
        mark that account and fail over to the next. Raises if all are spent."""
        last_err: Exception | None = None
        for agent in candidates:
            name = getattr(agent, "name", "?")
            if self._failover_enabled() and not self.registry.is_available(name):
                continue
            try:
                return make_call(agent)
            except Exception as e:
                if self._failover_enabled() and is_exhaustion_error(str(e)):
                    self.registry.mark_exhausted(name)
                    self._notify_failover(name)
                    last_err = e
                    continue
                raise
        if last_err is not None:
            raise last_err
        raise RuntimeError("No available accounts for this call (all rate/usage limited)")

    def _alternate_worker(self, step: Step, exclude: set[str]) -> tuple[str, BaseAgent] | None:
        try:
            return self._assign_worker(step, unavailable=exclude)
        except RuntimeError:
            return None

    def _worker_execute_with_failover(
        self,
        step: Step,
        worktree_path,
        output_callback,
        state: dict,
        room_id: str,
        *,
        plan: Plan | None = None,
        run_id: str = "",
        memory_summary: str | None = None,
    ) -> WorkerResult:
        """Run one worker turn; if the account is exhausted, mark it and retry on
        an alternate worker. `state` ({name, agent}) is updated in place so later
        turns and the runtime record reflect the account actually used."""
        tried: set[str] = set()
        while True:
            name, agent = state["name"], state["agent"]
            tried.add(name)
            worker = _make_worker(
                agent,
                (
                    memory_summary
                    if memory_summary is not None
                    else self._step_memory(step, plan, run_id)
                ),
                str(worktree_path),
                output_callback=output_callback,
            )
            result = worker.execute(step)
            exhausted = is_exhaustion_error(result.error)
            inactive = (
                "timed out" in (result.error or "").lower()
                or "no activity for" in (result.error or "").lower()
            )
            if result.success or not self._failover_enabled() or not (exhausted or inactive):
                return result

            if exhausted:
                self.registry.mark_exhausted(name)
            alt = self._alternate_worker(step, exclude=tried)
            if alt is None:
                if exhausted:
                    self._notify_failover(name, room_id)
                return result   # nobody left to take over — surface the failure
            alt_name, alt_agent = alt
            if exhausted:
                self._notify_failover(name, room_id, alt=alt_name)
            else:
                self._notify_timeout_failover(name, room_id, alt_name)
            state["name"], state["agent"] = alt_name, alt_agent

    @staticmethod
    def _dump_debug(label: str, content: str) -> None:
        """Write full content to genesis_debug.txt for diagnosis."""
        import os, datetime
        path = os.path.join(os.getcwd(), "genesis_debug.txt")
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n[{ts}] {label} ({len(content)} chars)\n{'='*60}\n")
                f.write(content)
                f.write("\n")
        except OSError:
            pass

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
    by_id: dict[str, Step] = {}
    for step in steps:
        if not step.step_id.strip():
            raise ValueError("Plan contains an empty step ID")
        if step.step_id in by_id:
            raise ValueError(f"Duplicate step ID: '{step.step_id}'")
        by_id[step.step_id] = step

    unknown_dependencies = sorted({
        dependency
        for step in steps
        for dependency in step.depends_on
        if dependency not in by_id
    })
    if unknown_dependencies:
        names = ", ".join(repr(dependency) for dependency in unknown_dependencies)
        raise ValueError(f"Unknown step dependencies: {names}")

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
