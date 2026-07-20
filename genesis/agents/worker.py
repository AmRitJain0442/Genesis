from __future__ import annotations
import logging
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from genesis.agents.base import BaseAgent
    from genesis.schemas.plan import Step

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a Genesis Worker Agent — a senior software engineer executing a single assigned step.

Your role:
- Read the step description and execute it precisely. Do not add unsolicited features.
- For code/test tasks: write complete, runnable code. No stubs, no placeholder comments.
- For docs/research tasks: write clear, accurate Markdown.
- For config tasks: write valid, complete config files.

OUTPUT FORMAT — mandatory:
Wrap your entire result in <result> tags.
For every file you create or modify, use a <code> block inside <result>:

  <code lang="LANGUAGE" file="PATH/TO/FILE.ext">
  full file contents here
  </code>

Example:
<result>
Creating a FastAPI application with health check and user endpoints.

<code lang="python" file="main.py">
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}
</code>

<code lang="text" file="requirements.txt">
fastapi==0.104.0
uvicorn==0.24.0
</code>
</result>

RULES:
- Always output complete file contents, never partial diffs.
- Multiple files → multiple <code> blocks inside a single <result>.
- State any significant assumptions briefly outside the code tags.
- Do not ask clarifying questions — make reasonable decisions and proceed.
- The memory context below shows what already exists; do not recreate it.
"""


@dataclass
class CodeBlock:
    language: str
    filename: str
    content: str


@dataclass
class WorkerResult:
    step_id: str
    raw_response: str
    result_text: str
    code_blocks: list[CodeBlock] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str = ""


class Worker:
    def __init__(self, agent: BaseAgent, memory_summary: str, work_dir: str = ".",
                 output_callback: Callable[[str], None] | None = None):
        self.agent = agent
        self.memory_summary = memory_summary
        self.work_dir = Path(work_dir).resolve()
        self.output_callback = output_callback

    def execute(self, step: Step) -> WorkerResult:
        user_msg = self._build_message(step)
        try:
            raw = self.agent.chat(
                _SYSTEM,
                [{"role": "user", "content": user_msg}],
                output_callback=self.output_callback,
            )
            return self._parse(raw, step)
        except Exception as e:
            logger.error("Worker error on %s: %s", step.step_id, e, exc_info=True)
            return WorkerResult(
                step_id=step.step_id,
                raw_response="",
                result_text="",
                success=False,
                error=str(e),
            )

    def _build_message(self, step: Step) -> str:
        return (
            f"MEMORY CONTEXT (what has been built so far):\n{self.memory_summary}\n\n"
            f"---\n\n"
            f"STEP DETAILS:\n"
            f"Step ID: {step.step_id}\n"
            f"Title: {step.title}\n"
            f"Type: {step.type}\n"
            f"Description: {step.description}\n"
            f"File Scope: {', '.join(step.file_scope) if step.file_scope else 'Unspecified'}\n"
            f"Expected Output: {step.expected_output}\n"
            f"Context Hint: {step.context_hint or 'None'}\n\n"
            f"Execute this step now. Remember to wrap your full response in <result> tags."
        )

    def _parse(self, raw: str, step: Step) -> WorkerResult:
        # Extract content between <result>…</result>
        m = re.search(r"<result>(.*?)</result>", raw, re.DOTALL)
        result_text = m.group(1).strip() if m else raw.strip()

        # Extract <code lang="…" file="…">…</code> blocks
        code_re = re.compile(
            r'<code\s+lang="([^"]+)"\s+file="([^"]+)">(.*?)</code>',
            re.DOTALL,
        )
        code_blocks: list[CodeBlock] = []
        files_written: list[str] = []
        pending_writes: list[tuple[CodeBlock, Path]] = []
        destinations: set[str] = set()

        for match in code_re.finditer(result_text):
            lang, filename, content = match.group(1), match.group(2), match.group(3)
            # Strip one leading newline that models typically add after the tag
            content = content.lstrip("\n")
            if not content.strip():
                logger.warning("Skipping empty code block for %s", filename)
                continue
            safe_filename, destination = self._confined_destination(filename)
            destination_key = os.path.normcase(str(destination))
            if destination_key in destinations:
                raise ValueError(f"duplicate worker output destination: {safe_filename}")
            destinations.add(destination_key)

            block = CodeBlock(
                language=lang,
                filename=safe_filename,
                content=content,
            )
            code_blocks.append(block)
            pending_writes.append((block, destination))

        # Validate every destination before touching the filesystem. This keeps
        # a later malicious or malformed code block from leaving earlier files
        # partially applied.
        for block, destination in pending_writes:
            self._atomic_write(destination, block.content)
            files_written.append(block.filename)
            logger.info("Wrote %s", block.filename)

        return WorkerResult(
            step_id=step.step_id,
            raw_response=raw,
            result_text=result_text,
            code_blocks=code_blocks,
            files_written=files_written,
            success=True,
        )

    def _confined_destination(self, filename: str) -> tuple[str, Path]:
        """Return a canonical relative name and destination inside work_dir."""
        candidate = filename
        if (
            not candidate
            or candidate != candidate.strip()
            or any(ord(char) < 32 for char in candidate)
        ):
            raise ValueError("worker output filename is empty or invalid")

        # Treat both slash styles as separators on every platform. Otherwise a
        # Windows path emitted while Genesis runs on POSIX (or vice versa) can
        # bypass the host platform's Path.is_absolute() check.
        portable = PurePosixPath(candidate.replace("\\", "/"))
        windows = PureWindowsPath(candidate)
        if portable.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
            raise ValueError(f"absolute worker output path is not allowed: {filename}")
        if ".." in portable.parts or ".." in windows.parts:
            raise ValueError(f"worker output path may not contain '..': {filename}")
        if any(":" in part for part in portable.parts):
            # On Windows this is an NTFS alternate-data-stream separator. Treat
            # it as unsafe on every platform so plans behave consistently.
            raise ValueError(f"worker output path contains an unsafe ':' component: {filename}")
        if os.name == "nt":
            reserved = {
                "CON", "PRN", "AUX", "NUL",
                *{f"COM{index}" for index in range(1, 10)},
                *{f"LPT{index}" for index in range(1, 10)},
            }
            for part in portable.parts:
                if part.rstrip(" .") != part:
                    raise ValueError(
                        f"worker output path has a Windows-ambiguous component: {filename}"
                    )
                if part.split(".", 1)[0].upper() in reserved:
                    raise ValueError(
                        f"worker output path uses a reserved device name: {filename}"
                    )

        relative = Path(*portable.parts)
        if relative == Path("."):
            raise ValueError(f"worker output path must name a file: {filename}")

        try:
            destination = (self.work_dir / relative).resolve(strict=False)
            canonical = destination.relative_to(self.work_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"worker output path escapes the work directory: {filename}"
            ) from exc

        if canonical == Path("."):
            raise ValueError(f"worker output path must name a file: {filename}")
        if destination.exists() and not destination.is_file():
            raise ValueError(f"worker output destination is not a file: {filename}")
        return canonical.as_posix(), destination

    def _atomic_write(self, destination: Path, content: str) -> None:
        """Durably stage content beside destination, then atomically replace it."""
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Resolve again after directory creation so an existing symlink or
        # junction cannot redirect a model-produced path outside the worktree.
        try:
            current_destination = destination.resolve(strict=False)
            current_destination.relative_to(self.work_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"worker output path escapes the work directory: {destination}"
            ) from exc
        if current_destination != destination:
            raise ValueError(f"worker output destination changed during validation: {destination}")

        existing_mode: int | None = None
        if destination.exists():
            if not destination.is_file():
                raise ValueError(f"worker output destination is not a file: {destination}")
            existing_mode = stat.S_IMODE(destination.stat().st_mode)

        descriptor: int | None = None
        temporary_path: Path | None = None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        try:
            for _ in range(100):
                temporary_path = destination.parent / (
                    f".{destination.name}.genesis-{secrets.token_hex(8)}.tmp"
                )
                try:
                    # Supplying 0o666 lets the process umask choose the normal
                    # mode for a new source file; mkstemp would force 0o600.
                    descriptor = os.open(temporary_path, flags, 0o666)
                    break
                except FileExistsError:
                    continue
            else:
                raise FileExistsError(
                    f"could not allocate a temporary file for {destination}"
                )

            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
