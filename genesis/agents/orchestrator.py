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
from genesis.verifier import Verifier, VerificationResult
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


@dataclass
class _StepExecution:
    step: Step
    worker_name: str
    worktree_path: Path | None = None
    result: WorkerResult | None = None
    patch: WorktreePatch | None = None
    patch_id: str = ""
    review: Review | None = None
    verification: VerificationResult | None = None
    repair_attempts: int = 0
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
- Break tasks into 3–10 concrete, atomic steps — each must produce a real artifact.
- Use depends_on to express ordering (step-2 depends on step-1 means step-1 runs first).
- Fill file_scope with concrete files/directories whenever known; use ["*"] for unclear, dependency, config, or repo-wide work.
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

    # ── Public API ─────────────────────────────────────────────────────────

    def plan(self, task: str, on_status=None) -> Plan:
        def status(msg: str) -> None:
            if on_status:
                try:
                    on_status(msg)
                except Exception:
                    pass

        mem = self.memory.get_summary(self.config.memory.max_context_chars)
        palace_mem = ""
        if self.palace and self.config.memory.palace_enabled:
            try:
                palace_mem = self.palace.wakeup_context(
                    task,
                    max_chars=self.config.memory.max_context_chars,
                    wing=str(Path(self.work_dir).resolve()),
                )
            except Exception as e:
                logger.warning("Palace wakeup failed: %s", e)
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

            max_rounds = collab_cfg.max_rounds if collab_cfg else 4
            collab = BrainCollaboration(
                self.agent, self.co_brain, chatroom=self.chatroom, max_rounds=max_rounds
            )
            result = collab.discuss(task, context=context, on_status=on_status)
        except Exception as e:
            logger.warning("Brain debate failed, falling back to single-brain plan: %s", e)
            return ""

        if on_status:
            try:
                on_status("Brains reached consensus" if result.converged
                          else "Round cap reached - arbitrating the plan")
            except Exception:
                pass

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
        project_memory = self.memory.get_summary(
            min(6000, self.config.memory.max_context_chars)
        )
        worker_summary = (result.result_text or "No worker summary captured.")[:3000]

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
                    cb = callbacks or {}
                    if fn := cb.get("on_status"):
                        fn(f"Reusing saved plan {saved.run_id}; planning is already complete.")
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
                    if fn := cb.get("on_plan"):
                        fn(plan)
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
                    if on_status:
                        on_status(f"Reusing saved plan {saved.run_id}.")
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
        cb = callbacks or {}

        def fire(name: str, *args, **kwargs) -> None:
            if fn := cb.get(name):
                fn(*args, **kwargs)

        fire("on_status", "Planning task...")
        plan = self.plan(task, on_status=lambda m: fire("on_status", m))
        if not plan.steps:
            raise ValueError("Orchestrator returned an empty plan - no steps to execute.")
        fire("on_plan", plan)

        self._save_plan(task, plan, status="running")
        self._execute_plan_isolated(plan, callbacks=callbacks)

    def _save_plan(self, task: str, plan: Plan, *, status: str) -> None:
        run_id = plan.task_id
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
            for step in _topo_sort(plan.steps):
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="pending",
                    metadata={"step": step.model_dump()},
                )
            self.runtime.update_run_status(run_id, status)

        if self.config.memory.auto_append_plan:
            self.memory.append_plan(plan)
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
        if callbacks and (fn := callbacks.get("on_plan")):
            fn(plan)
        self._execute_plan_isolated(plan, callbacks=callbacks)

    def _execute_plan_isolated(
        self,
        plan: Plan,
        *,
        callbacks: dict[str, Callable] | None = None,
    ) -> None:
        cb = callbacks or {}
        output_callback = cb.get("on_output")
        output_lock = Lock()
        worktree_lock = Lock()

        def fire(name: str, *args, **kwargs) -> None:
            if fn := cb.get(name):
                fn(*args, **kwargs)

        def guarded_output(text: str) -> None:
            if output_callback:
                with output_lock:
                    output_callback(text)

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
                committed_ids.add(step.step_id)
                fire("on_status", f"Skipping committed {step.step_id}")
            elif record.status == "blocked":
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
        halted_new_work = False

        fire(
            "on_status",
            f"Executing {len(steps)} steps with up to {max_parallel} parallel worker(s)...",
        )
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            while completed < len(steps):
                if not halted_new_work:
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
                            guarded_output,
                            worktree_lock,
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
                        if execution.patch:
                            self.memory.append_step(
                                step.step_id,
                                step.title,
                                execution.worker_name,
                                execution.review.memory_note,
                                execution.review.verdict,
                            )
                            self._remember_step(
                                run_id,
                                step,
                                execution.worker_name,
                                execution.review,
                                execution.patch,
                            )

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
                        halted_new_work = True
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
                        halted_new_work = True
                        continue

                    try:
                        worktrees.apply_check(execution.patch.patch_text)
                        worktrees.apply_patch(execution.patch.patch_text)
                    except Exception as e:
                        self._block_step(run_id, step, "git-apply", f"Patch apply failed: {e}", str(e), fire)
                        blocked_ids.add(step.step_id)
                        halted_new_work = True
                        continue

                    sha = self.git.commit_step(step.step_id, step.title)
                    if not sha and execution.patch.has_changes:
                        self._block_step(
                            run_id,
                            step,
                            "git",
                            "Commit failed after applying approved patch.",
                            "commit failed",
                            fire,
                        )
                        blocked_ids.add(step.step_id)
                        halted_new_work = True
                        continue
                    if sha and self.config.git.auto_push:
                        self.git.push()
                    fire("on_commit", step, sha)
                    if self.runtime:
                        self.runtime.upsert_step(
                            run_id,
                            step.step_id,
                            title=step.title,
                            status="committed",
                            commit_sha=sha or "",
                            metadata=self._scope_metadata(
                                step,
                                scheduled.scope,
                                lease="released",
                                extra={
                                    "reviewer": execution.reviewer_name,
                                    "review_verdict": execution.review.verdict if execution.review else "",
                                    "repair_attempts": execution.repair_attempts,
                                },
                            ),
                        )
                    if execution.worktree_path:
                        try:
                            worktrees.remove(execution.worktree_path)
                        except Exception as e:
                            logger.warning("Could not remove worktree %s: %s", execution.worktree_path, e)

                    committed_ids.add(step.step_id)
                    completed += 1
                    fire("on_step_complete", step, execution.review, completed, len(steps))

                if halted_new_work and not active:
                    break

        if completed == len(steps) and not blocked_ids:
            release_summary = self._release_summary(plan, completed, len(steps))
            self.memory.complete_task(plan.task_id)
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
    ) -> _StepExecution:
        execution = _StepExecution(step=step, worker_name=worker_name)
        execution.reviewer_name = self._reviewer_name()
        try:
            record = self.runtime.get_step(run_id, step.step_id) if self.runtime else None
            worktree_path = Path(record.worktree_path) if record and record.worktree_path else None
            if not worktree_path or not worktree_path.exists():
                with worktree_lock:
                    worktree_path = worktrees.create(run_id, step.step_id)
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

            def run_turn(s):
                return self._worker_execute_with_failover(
                    s,
                    worktree_path,
                    output_callback,
                    worker_state,
                    step_room,
                    plan=plan,
                    run_id=run_id,
                )

            # Phase 3b: a multi-turn brain<->worker dialogue shapes the work
            # before the independent reviewer gates it. Single-shot when disabled.
            if getattr(self.config, "dialogue", None) and self.config.dialogue.enabled:
                outcome = self._run_worker_dialogue(
                    step, run_turn, worker_state["name"], step_room, output_callback
                )
                result = outcome.result
            else:
                result = run_turn(step)
                self._post_step(
                    step_room, worker_state["name"], "worker",
                    f"Wrote {len(getattr(result, 'files_written', []) or [])} file(s)", "code",
                )

            # Failover may have switched the account partway through.
            worker_name = worker_state["name"]
            execution.worker_name = worker_name
            execution.result = result
            if result is None or not result.success:
                reason = (getattr(result, "error", "") if result else "") or "worker failed"
                execution.failed_agent = worker_name
                execution.failed_reason = reason
                execution.memory_note = f"FAILED: {reason}"
                self._post_step(step_room, worker_name, "worker", f"Failed: {reason}", "status")
                return execution

            patch = worktrees.capture_patch(worktree_path)
            # The captured patch is the authoritative review manifest. Never
            # leave a final-turn-only file list attached to an empty patch.
            result.files_written = patch.changed_files
            execution.patch = patch
            execution.patch_id = self._store_patch(run_id, step.step_id, patch)
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="reviewing",
                    patch_artifact_id=execution.patch_id,
                    metadata={"changed_files": patch.changed_files},
                )
                self.runtime.record_event(
                    run_id,
                    "worker_finished",
                    step_id=step.step_id,
                    payload={
                        "worker": worker_name,
                        "changed_files": patch.changed_files,
                        "repair_attempts": execution.repair_attempts,
                    },
                )

            if not patch.has_changes:
                execution.failed_agent = worker_name
                execution.failed_reason = (
                    "Worker completed without a reviewable patch. No reviewer "
                    "was called because the changed-file manifest is empty."
                )
                execution.memory_note = execution.failed_reason
                self._post_step(
                    step_room,
                    worker_name,
                    "worker",
                    execution.failed_reason,
                    "status",
                )
                return execution

            review = self.review(
                step,
                result,
                plan=plan,
                run_id=run_id,
                work_dir=worktree_path,
                diff_text=patch.patch_text or "No git diff available.",
            )
            execution.review = review
            self._post_step(
                step_room, execution.reviewer_name, "reviewer",
                f"{review.verdict} ({review.quality_score}/10): {review.feedback}",
                "decision",
            )
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="reviewing",
                    review=review.model_dump(),
                    metadata={
                        "reviewer": execution.reviewer_name,
                        "review_verdict": review.verdict,
                        "repair_attempts": execution.repair_attempts,
                    },
                )

            self._record_review(
                run_id,
                step,
                review,
                reviewer=execution.reviewer_name,
                repair_attempts=execution.repair_attempts,
            )

            retries_left = max(0, self.config.runtime.retry_budget)
            while review.verdict == "needs_revision" and review.should_retry and retries_left:
                retries_left -= 1
                execution.repair_attempts += 1
                reason = review.suggested_revision or review.feedback or review.verdict
                if output_callback:
                    output_callback(f"Retrying {step.step_id}: {reason}")
                self._record_repair_attempt(
                    run_id,
                    step,
                    reason=reason,
                    attempts_used=execution.repair_attempts,
                    attempts_left=retries_left,
                )
                revised = self._revision_step(step, reason)
                result = run_turn(revised)
                execution.result = result
                if not result.success:
                    review = review.model_copy(update={
                        "verdict": "rejected",
                        "quality_score": 1,
                        "feedback": result.error,
                        "memory_note": f"Revision failed: {result.error}",
                        "should_retry": False,
                    })
                    execution.review = review
                    break
                patch = worktrees.capture_patch(worktree_path)
                result.files_written = patch.changed_files
                execution.patch = patch
                execution.patch_id = self._store_patch(run_id, step.step_id, patch)
                if self.runtime:
                    self.runtime.record_event(
                        run_id,
                        "worker_finished",
                        step_id=step.step_id,
                        payload={
                            "worker": worker_name,
                            "changed_files": patch.changed_files,
                            "repair_attempts": execution.repair_attempts,
                        },
                    )
                review = self.review(
                    revised,
                    result,
                    plan=plan,
                    run_id=run_id,
                    work_dir=worktree_path,
                    diff_text=patch.patch_text or "No git diff available.",
                )
                execution.review = review
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
                            "repair_attempts": execution.repair_attempts,
                        },
                    )
                self._record_review(
                    run_id,
                    step,
                    review,
                    reviewer=execution.reviewer_name,
                    repair_attempts=execution.repair_attempts,
                )

            if review.verdict != "approved":
                execution.failed_agent = worker_name
                execution.failed_reason = review.feedback or review.verdict
                execution.memory_note = review.memory_note
                return execution

            if self.runtime:
                self.runtime.upsert_step(run_id, step.step_id, title=step.title, status="verifying")
            verifier = Verifier(
                self.config,
                self.policy or ExecutionPolicy.load(self.work_dir, self.config),
                str(worktree_path),
                output_callback=output_callback,
            )
            verification = verifier.verify(changed_files=execution.patch.changed_files if execution.patch else [])
            execution.verification = verification
            self._record_verification(run_id, step.step_id, verification)
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="verifying",
                    verification=self._verification_payload(verification),
                    metadata={"repair_attempts": execution.repair_attempts},
                )
            if not verification.passed:
                while retries_left:
                    retries_left -= 1
                    execution.repair_attempts += 1
                    reason = self._verification_summary(verification)
                    if output_callback:
                        output_callback(f"Repairing {step.step_id} after verification failure")
                    self._record_repair_attempt(
                        run_id,
                        step,
                        reason=reason,
                        attempts_used=execution.repair_attempts,
                        attempts_left=retries_left,
                    )
                    revised = self._revision_step(step, reason)
                    result = run_turn(revised)
                    execution.result = result
                    if not result.success:
                        execution.failed_agent = worker_name
                        execution.failed_reason = result.error or "worker failed during verification repair"
                        execution.memory_note = f"Verification repair failed: {execution.failed_reason}"
                        return execution

                    patch = worktrees.capture_patch(worktree_path)
                    result.files_written = patch.changed_files
                    execution.patch = patch
                    execution.patch_id = self._store_patch(run_id, step.step_id, patch)
                    if self.runtime:
                        self.runtime.record_event(
                            run_id,
                            "worker_finished",
                            step_id=step.step_id,
                            payload={
                                "worker": worker_name,
                                "changed_files": patch.changed_files,
                                "repair_attempts": execution.repair_attempts,
                            },
                        )
                        self.runtime.upsert_step(
                            run_id,
                            step.step_id,
                            title=step.title,
                            status="reviewing",
                            patch_artifact_id=execution.patch_id,
                            metadata={
                                "changed_files": patch.changed_files,
                                "repair_attempts": execution.repair_attempts,
                            },
                        )

                    review = self.review(
                        revised,
                        result,
                        plan=plan,
                        run_id=run_id,
                        work_dir=worktree_path,
                        diff_text=patch.patch_text or "No git diff available.",
                    )
                    execution.review = review
                    if self.runtime:
                        self.runtime.upsert_step(
                            run_id,
                            step.step_id,
                            title=step.title,
                            status="reviewing",
                            review=review.model_dump(),
                        )
                    self._record_review(
                        run_id,
                        step,
                        review,
                        reviewer=execution.reviewer_name,
                        repair_attempts=execution.repair_attempts,
                    )
                    if review.verdict != "approved":
                        execution.failed_agent = worker_name
                        execution.failed_reason = review.feedback or review.verdict
                        execution.memory_note = review.memory_note
                        return execution

                    if self.runtime:
                        self.runtime.upsert_step(run_id, step.step_id, title=step.title, status="verifying")
                    verification = verifier.verify(changed_files=execution.patch.changed_files if execution.patch else [])
                    execution.verification = verification
                    self._record_verification(run_id, step.step_id, verification)
                    if self.runtime:
                        self.runtime.upsert_step(
                            run_id,
                            step.step_id,
                            title=step.title,
                            status="verifying",
                            verification=self._verification_payload(verification),
                            metadata={"repair_attempts": execution.repair_attempts},
                        )
                    if verification.passed:
                        break

                if not verification.passed:
                    if self.config.verification.require_for_commit:
                        execution.failed_agent = "verifier"
                        execution.failed_reason = verification.reason
                        execution.memory_note = f"Verification failed: {verification.reason}"
                        return execution
                    # Advisory mode: surface the failure but allow the commit.
                    if output_callback:
                        output_callback(
                            f"Verification failed but require_for_commit=false; "
                            f"committing anyway: {verification.reason}"
                        )

            return execution
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
        msg = (
            f"STEP:\n  ID: {step.step_id}\n  Title: {step.title}\n  Type: {step.type}\n"
            f"  Expected Output: {step.expected_output}\n\n"
            f"WORKER FILES ({len(files)}): {', '.join(files) or 'none'}\n\n"
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

    def _run_worker_dialogue(self, step: Step, run_worker, worker_name: str,
                             step_room: str, output_callback):
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

        lines = [
            f"Original task: {original_task}",
            f"Plan summary: {plan.task_summary}",
            "Retained plan and current state:",
        ]
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
        mem_summary = self.memory.get_summary(self.config.memory.max_context_chars)
        if self.palace and self.config.memory.palace_enabled:
            palace_context = self.palace.wakeup_context(
                f"{step.title}\n{step.description}",
                max_chars=self.config.memory.max_context_chars,
                wing=str(Path(self.work_dir).resolve()),
            )
            if palace_context:
                mem_summary = mem_summary + "\n\n---\n\n" + palace_context
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
            },
        )

    def _remember_step(
        self,
        run_id: str,
        step: Step,
        worker_name: str,
        review: Review,
        patch: WorktreePatch,
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
                f"Patch:\n{patch.patch_text}"
            ),
            status=review.verdict,
            metadata={"worker": worker_name, "files": patch.changed_files},
        )

    def _record_review(
        self,
        run_id: str,
        step: Step,
        review: Review,
        *,
        reviewer: str,
        repair_attempts: int,
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
                "repair_attempts": repair_attempts,
            },
        )

    def _record_repair_attempt(
        self,
        run_id: str,
        step: Step,
        *,
        reason: str,
        attempts_used: int,
        attempts_left: int,
    ) -> None:
        if not self.runtime:
            return
        self.runtime.record_event(
            run_id,
            "repair_attempted",
            step_id=step.step_id,
            payload={
                "reason": reason,
                "attempts_used": attempts_used,
                "attempts_left": attempts_left,
            },
        )
        self.runtime.upsert_step(
            run_id,
            step.step_id,
            title=step.title,
            metadata={
                "repair_attempts": attempts_used,
                "last_repair_reason": reason,
            },
        )

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

    def _verification_payload(self, verification: VerificationResult) -> dict:
        return {
            "passed": verification.passed,
            "skipped": verification.skipped,
            "reason": verification.reason,
            "commands": [
                {
                    "command": cmd.command,
                    "returncode": cmd.returncode,
                    "output": cmd.output,
                }
                for cmd in verification.commands
            ],
        }

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
        self.memory.append_step(step.step_id, step.title, agent, memory_note, "rejected")
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
    ) -> None:
        payload = {
            "passed": verification.passed,
            "skipped": verification.skipped,
            "reason": verification.reason,
            "commands": [
                {
                    "command": cmd.command,
                    "returncode": cmd.returncode,
                    "output": cmd.output,
                }
                for cmd in verification.commands
            ],
        }
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
    ) -> WorkerResult:
        """Run one worker turn; if the account is exhausted, mark it and retry on
        an alternate worker. `state` ({name, agent}) is updated in place so later
        turns and the runtime record reflect the account actually used."""
        tried: set[str] = set()
        while True:
            name, agent = state["name"], state["agent"]
            tried.add(name)
            worker = _make_worker(
                agent, self._step_memory(step, plan, run_id), str(worktree_path),
                output_callback=output_callback,
            )
            result = worker.execute(step)
            if result.success or not self._failover_enabled() or not is_exhaustion_error(result.error):
                return result

            self.registry.mark_exhausted(name)
            alt = self._alternate_worker(step, exclude=tried)
            if alt is None:
                self._notify_failover(name, room_id)
                return result   # nobody left to take over — surface the exhaustion
            alt_name, alt_agent = alt
            self._notify_failover(name, room_id, alt=alt_name)
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
