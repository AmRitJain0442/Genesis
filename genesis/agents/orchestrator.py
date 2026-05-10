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
from genesis.palace import PalaceStore
from genesis.policy import ExecutionPolicy
from genesis.runtime import RuntimeStore
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
        runtime: RuntimeStore | None = None,
        palace: PalaceStore | None = None,
        policy: ExecutionPolicy | None = None,
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

    # ── Public API ─────────────────────────────────────────────────────────

    def plan(self, task: str) -> Plan:
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
        base_msg = (
            f"CURRENT MEMORY CONTEXT:\n{mem}\n\n---\n\n"
            f"RETRIEVED PALACE MEMORY:\n{palace_mem or 'No relevant palace memories.'}\n\n---\n\n"
            f"TASK TO PLAN:\n{task}\n\n"
            f"Return the plan as JSON. Output ONLY the JSON object — "
            f"start your response with {{ and end with }}. No preamble, no explanation."
        )

        last_err: Exception | None = None
        for attempt in range(2):
            msg = base_msg if attempt == 0 else (
                base_msg + "\n\nIMPORTANT: your previous response could not be parsed. "
                "Output ONLY the raw JSON object. No prose, no markdown fences, no code blocks. "
                "Begin with { and end with }."
            )
            if hasattr(self.agent, "chat_plan"):
                raw = self.agent.chat_plan(_SYSTEM, [{"role": "user", "content": msg}])
            else:
                raw = self.agent.chat(_SYSTEM, [{"role": "user", "content": msg}])

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

    def review(
        self,
        step: Step,
        result: WorkerResult,
        *,
        work_dir: str | Path | None = None,
        diff_text: str | None = None,
    ) -> Review:
        # Read actual file contents so Claude reviews real code, not just a summary.
        # Cap per-file at 3 KB and total file content at 10 KB.
        _PER_FILE = 3000
        _TOTAL_CAP = 10000
        file_sections: list[str] = []
        total = 0
        review_dir = Path(work_dir or self.work_dir)
        for fname in result.files_written:
            fpath = review_dir / fname
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

        diff_block = diff_text if diff_text is not None else self.git.diff_text(result.files_written, max_chars=12000)
        if not diff_block:
            diff_block = "No git diff available."

        msg = (
            f"Review this step result.\n\n"
            f"STEP:\n"
            f"  ID: {step.step_id}\n"
            f"  Title: {step.title}\n"
            f"  Type: {step.type}\n"
            f"  Expected Output: {step.expected_output}\n\n"
            f"FILES WRITTEN ({len(result.files_written)}):\n\n{files_block}\n\n"
            f"GIT DIFF:\n\n{diff_block}\n\n"
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
        self._run_fresh_task(task, callbacks)
        return

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
        plan = self.plan(task)
        if not plan.steps:
            raise ValueError("Orchestrator returned an empty plan - no steps to execute.")
        fire("on_plan", plan)

        run_id = plan.task_id
        if self.runtime:
            self.runtime.start_run(
                plan.task_summary,
                run_id=run_id,
                metadata={"estimated_steps": plan.estimated_steps},
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
        self._execute_plan_isolated(plan, callbacks=callbacks)

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
        if retry_step_id:
            self.runtime.reset_step_for_retry(run_id, retry_step_id)
        else:
            self.runtime.update_run_status(run_id, "running")
        plan = Plan(**plan_payload)
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

        def fire(name: str, *args, **kwargs) -> None:
            if fn := cb.get(name):
                fn(*args, **kwargs)

        if not self.config.git.auto_commit:
            raise RuntimeError(
                "Isolated execution requires git.auto_commit=true so each accepted "
                "step becomes the base for the next worktree."
            )

        run_id = plan.task_id
        steps = _topo_sort(plan.steps)
        worktrees = WorktreeManager(self.work_dir)
        worktrees.ensure_clean_main(ignore_paths=[self.config.memory.file, ".genesis/"])
        completed = sum(
            1
            for step in steps
            if self.runtime
            and (record := self.runtime.get_step(run_id, step.step_id))
            and record.status == "committed"
        )
        blocked = False

        for i, step in enumerate(steps):
            record = self.runtime.get_step(run_id, step.step_id) if self.runtime else None
            if record and record.status == "committed":
                fire("on_status", f"Skipping committed {step.step_id}")
                continue

            fire("on_step_start", step, i, len(steps))
            if self.runtime:
                self.runtime.upsert_step(run_id, step.step_id, title=step.title, status="running")

            worker_name, worker_agent = self._assign_worker(step)
            fire("on_worker_assigned", step, worker_name)

            worktree_path = Path(record.worktree_path) if record and record.worktree_path else None
            if not worktree_path or not worktree_path.exists():
                worktree_path = worktrees.create(run_id, step.step_id)
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="running",
                    worker=worker_name,
                    worktree_path=str(worktree_path),
                )

            worker = _make_worker(
                worker_agent,
                self._step_memory(step),
                str(worktree_path),
                output_callback=output_callback,
            )
            result = worker.execute(step)
            if not result.success:
                self._block_step(run_id, step, worker_name, f"FAILED: {result.error}", result.error, fire)
                blocked = True
                break

            patch = worktrees.capture_patch(worktree_path)
            if patch.changed_files:
                result.files_written = patch.changed_files
            patch_id = self._store_patch(run_id, step.step_id, patch)
            fire("on_step_result", step, result, worker_name)
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="reviewing",
                    patch_artifact_id=patch_id,
                    metadata={"changed_files": patch.changed_files},
                )

            fire("on_status", f"Reviewing {step.step_id}...")
            review = self.review(
                step,
                result,
                work_dir=worktree_path,
                diff_text=patch.patch_text or "No git diff available.",
            )
            fire("on_review", step, review)
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="reviewing",
                    review=review.model_dump(),
                )

            retries_left = max(0, self.config.runtime.retry_budget)
            while review.verdict == "needs_revision" and review.should_retry and retries_left:
                retries_left -= 1
                fire("on_status", f"Retrying {step.step_id} with feedback...")
                revised = step.model_copy(update={
                    "description": step.description + f"\n\nREVISION REQUIRED: {review.suggested_revision}"
                })
                result = worker.execute(revised)
                if not result.success:
                    review = review.model_copy(update={
                        "verdict": "rejected",
                        "quality_score": 1,
                        "feedback": result.error,
                        "memory_note": f"Revision failed: {result.error}",
                        "should_retry": False,
                    })
                    break
                patch = worktrees.capture_patch(worktree_path)
                if patch.changed_files:
                    result.files_written = patch.changed_files
                patch_id = self._store_patch(run_id, step.step_id, patch)
                fire("on_step_result", step, result, worker_name)
                review = self.review(
                    revised,
                    result,
                    work_dir=worktree_path,
                    diff_text=patch.patch_text or "No git diff available.",
                )
                fire("on_review", step, review)
                if self.runtime:
                    self.runtime.upsert_step(
                        run_id,
                        step.step_id,
                        title=step.title,
                        status="reviewing",
                        patch_artifact_id=patch_id,
                        review=review.model_dump(),
                    )

            self.memory.append_step(step.step_id, step.title, worker_name, review.memory_note, review.verdict)
            self._remember_step(run_id, step, worker_name, review, patch)
            if review.verdict != "approved":
                self._block_step(
                    run_id,
                    step,
                    worker_name,
                    review.memory_note,
                    review.feedback or review.verdict,
                    fire,
                )
                blocked = True
                break

            if self.runtime:
                self.runtime.upsert_step(run_id, step.step_id, title=step.title, status="verifying")
            verifier = Verifier(
                self.config,
                self.policy or ExecutionPolicy.load(self.work_dir, self.config),
                str(worktree_path),
                output_callback=output_callback,
            )
            verification = verifier.verify(changed_files=patch.changed_files)
            self._record_verification(run_id, step.step_id, verification)
            if self.runtime:
                self.runtime.upsert_step(
                    run_id,
                    step.step_id,
                    title=step.title,
                    status="verifying",
                    verification=self._verification_payload(verification),
                )
            if not verification.passed:
                self._block_step(
                    run_id,
                    step,
                    "verifier",
                    f"Verification failed: {verification.reason}",
                    verification.reason,
                    fire,
                )
                blocked = True
                break

            try:
                worktrees.apply_check(patch.patch_text)
                worktrees.apply_patch(patch.patch_text)
            except Exception as e:
                self._block_step(run_id, step, "git-apply", f"Patch apply failed: {e}", str(e), fire)
                blocked = True
                break

            sha = self.git.commit_step(step.step_id, step.title)
            if not sha and patch.has_changes:
                self._block_step(
                    run_id,
                    step,
                    "git",
                    "Commit failed after applying approved patch.",
                    "commit failed",
                    fire,
                )
                blocked = True
                break
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
                )
            try:
                worktrees.remove(worktree_path)
            except Exception as e:
                logger.warning("Could not remove worktree %s: %s", worktree_path, e)

            completed += 1
            fire("on_step_complete", step, review, completed, len(steps))

        if not blocked:
            self.memory.complete_task(plan.task_id)
            if self.runtime:
                self.runtime.update_run_status(
                    run_id,
                    "completed",
                    metadata={"completed_steps": completed, "total_steps": len(steps)},
                )
            self._palace_add(
                run_id=run_id,
                step_id="",
                closet="runs",
                kind="run-summary",
                title=f"Completed: {plan.task_summary}",
                content=f"Completed {completed}/{len(steps)} steps.",
                status="completed",
            )
            sha = self.git.commit_step("task-complete", plan.task_summary[:60])
            if sha and self.config.git.auto_push:
                self.git.push()
            fire("on_task_complete", plan)

    def _step_memory(self, step: Step) -> str:
        mem_summary = self.memory.get_summary(self.config.memory.max_context_chars)
        if self.palace and self.config.memory.palace_enabled:
            palace_context = self.palace.wakeup_context(
                f"{step.title}\n{step.description}",
                max_chars=self.config.memory.max_context_chars,
                wing=str(Path(self.work_dir).resolve()),
            )
            if palace_context:
                mem_summary = mem_summary + "\n\n---\n\n" + palace_context
        return mem_summary

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
                metadata={"blocked_reason": reason},
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
                "verification",
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
