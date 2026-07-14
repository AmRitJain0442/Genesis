"""
CodexWorker — runs a step using the Codex CLI as an autonomous executor.

Unlike the standard Worker (which parses XML and writes files manually),
CodexWorker lets Codex write files directly to disk. After execution it
reads the git diff to discover which files were created or modified.
"""
from __future__ import annotations
import logging
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from genesis.agents.codex_cli import CodexCLIAgent
    from genesis.schemas.plan import Step

from genesis.agents.worker import WorkerResult

logger = logging.getLogger(__name__)

_WORKER_PROMPT_TEMPLATE = """\
You are an expert software engineer executing a single task.

MEMORY (what has already been built — do not duplicate this work):
{memory_summary}

---

TASK:
Step ID:         {step_id}
Title:           {title}
Type:            {type}
Description:
{description}

File scope:      {file_scope}
Expected output: {expected_output}
Context hint:    {context_hint}

Instructions:
- Implement this step completely. Write production-quality code — no stubs.
- Create all necessary files in the current working directory.
- If you need to install packages or run setup commands, do so.
- When finished, write a short summary (2-4 sentences) of exactly what you created or changed.
"""


class CodexWorker:
    """
    Executes a step by handing off the full prompt to a CodexCLIAgent.
    Codex writes files directly; we use git to detect what changed.
    """

    def __init__(
        self,
        agent: CodexCLIAgent,
        memory_summary: str,
        work_dir: str = ".",
        output_callback: Callable[[str], None] | None = None,
    ):
        self.agent = agent
        self.memory_summary = memory_summary
        self.work_dir = Path(work_dir).resolve()
        self.output_callback = output_callback

    def execute(self, step: Step) -> WorkerResult:
        prompt = _WORKER_PROMPT_TEMPLATE.format(
            memory_summary=self.memory_summary,
            step_id=step.step_id,
            title=step.title,
            type=step.type,
            description=step.description,
            file_scope=", ".join(step.file_scope) if step.file_scope else "Unspecified",
            expected_output=step.expected_output,
            context_hint=step.context_hint or "None",
        )

        # Snapshot file content before execution. This is more reliable than
        # mtimes and catches quick rewrites on filesystems with coarse clocks.
        before = self._snapshot()

        try:
            # Always use streaming execution so the inactivity watchdog can
            # observe progress even when no UI callback is configured.
            callback = self.output_callback or (lambda _message: None)
            summary = self.agent.execute_task(prompt, output_callback=callback)
        except Exception as e:
            files_written = self._diff(before)
            is_timeout = (
                "timed out" in str(e).lower()
                or "no activity for" in str(e).lower()
            )
            if is_timeout and files_written:
                logger.warning(
                    "Codex became inactive after changing %d file(s); preserving partial work",
                    len(files_written),
                )
                return WorkerResult(
                    step_id=step.step_id,
                    raw_response="",
                    result_text=(
                        f"Codex became inactive, but {len(files_written)} changed "
                        "file(s) were preserved for review and continuation."
                    ),
                    files_written=files_written,
                    success=True,
                )
            logger.error("CodexWorker error on %s: %s", step.step_id, e, exc_info=True)
            return WorkerResult(
                step_id=step.step_id,
                raw_response="",
                result_text="",
                success=False,
                error=str(e),
            )

        # Discover what changed
        files_written = self._diff(before)
        logger.info("Codex wrote %d file(s): %s", len(files_written), files_written)

        return WorkerResult(
            step_id=step.step_id,
            raw_response=summary,
            result_text=summary,
            files_written=files_written,
            success=True,
        )

    # ── File change detection ──────────────────────────────────────────────

    def _snapshot(self) -> dict[str, str]:
        """Return {relative_path: sha256} for all relevant files in work_dir."""
        snap: dict[str, str] = {}
        for relative in self._git_visible_files():
            p = self.work_dir / relative
            if not p.is_file():
                continue
            try:
                snap[relative] = hashlib.sha256(p.read_bytes()).hexdigest()
            except Exception:
                pass
        return snap

    def _git_visible_files(self) -> list[str]:
        """Tracked and non-ignored untracked files, excluding cache noise."""
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.work_dir),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                check=True,
            )
            paths = result.stdout.decode("utf-8", errors="replace").split("\0")
            return sorted(
                path.replace("\\", "/")
                for path in paths
                if path and not _is_ignored(self.work_dir / path, self.work_dir)
            )
        except (OSError, subprocess.SubprocessError):
            return sorted(
                str(path.relative_to(self.work_dir)).replace("\\", "/")
                for path in self.work_dir.rglob("*")
                if path.is_file() and not _is_ignored(path, self.work_dir)
            )

    def _diff(self, before: dict[str, str]) -> list[str]:
        """Return files that are new or modified since the snapshot."""
        after = self._snapshot()
        return sorted(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )


def _is_ignored(path: Path, root: Path) -> bool:
    """Skip git internals, caches, and binary blobs."""
    rel = str(path.relative_to(root)).replace("\\", "/")
    parts = Path(rel).parts
    skip_parts = (
        ".git",
        ".genesis",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
    )
    skip_suffixes = (".pyc", ".pyo", ".egg-info")
    return (
        any(part in skip_parts for part in parts)
        or any(rel.endswith(s) for s in skip_suffixes)
    )
