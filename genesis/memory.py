from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from genesis.schemas.plan import Plan

_HEADER = """\
# GENESIS MEMORY
*AI Orchestration System — Shared Project Memory*

---

"""

_UTF8_MAX_BYTES = 4
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    """Return the process-wide lock shared by every manager for ``path``."""
    key = os.path.normcase(str(path.expanduser().resolve(strict=False)))
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _sync(stream: TextIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())


class MemoryManager:
    def __init__(self, file_path: str):
        self.path = Path(file_path)
        self._lock = _path_lock(self.path)
        with self._lock:
            self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        """Create a complete header once without truncating an existing file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = self.path.open("x", encoding="utf-8", newline="\n")
        except FileExistsError:
            if not self.path.is_file():
                raise IsADirectoryError(f"Memory path is not a file: {self.path}")
            return

        try:
            with stream:
                stream.write(_HEADER)
                _sync(stream)
        except BaseException:
            # A failed exclusive creation must not leave a partial header that a
            # later manager would mistake for initialized memory.
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def read(self) -> str:
        with self._lock:
            self._ensure_initialized()
            with self.path.open("r", encoding="utf-8", errors="replace") as stream:
                return stream.read()

    def get_summary(self, max_chars: int = 6000) -> str:
        """Return a bounded UTF-8-safe tail, trimmed to a paragraph boundary."""
        if max_chars <= 0:
            return ""

        with self._lock:
            self._ensure_initialized()
            truncated, content = self._read_tail(max_chars)

        if not truncated:
            return content

        # Prefer a paragraph break (double newline) to avoid splitting mid-section.
        idx = content.find("\n\n")
        if idx > 0:
            content = content[idx + 2 :]
        else:
            idx = content.find("\n")
            if idx > 0:
                content = content[idx + 1 :]
        return "[...earlier context truncated...]\n\n" + content

    def _read_tail(self, max_chars: int) -> tuple[bool, str]:
        """Read at most four UTF-8 bytes per requested character, plus boundary slack."""
        with self.path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            read_size = min(size, max_chars * _UTF8_MAX_BYTES + _UTF8_MAX_BYTES)
            start = size - read_size
            stream.seek(start)
            data = stream.read(read_size)

        # A byte seek may land in the middle of a UTF-8 code point. Discard only
        # its leading continuation bytes; the complete suffix remains untouched.
        if start:
            boundary = 0
            while boundary < len(data) and data[boundary] & 0xC0 == 0x80:
                boundary += 1
            data = data[boundary:]

        # Match text-mode ``read`` semantics for files created on any platform.
        content = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        truncated = start > 0 or len(content) > max_chars
        if len(content) > max_chars:
            content = content[-max_chars:]
        return truncated, content

    def append_plan(self, plan: Plan) -> None:
        ts = _now()
        lines = [
            f"\n## Task: {plan.task_summary}",
            f"*Started: {ts}* · Task ID: `{plan.task_id}`\n",
            f"### Plan ({plan.estimated_steps} steps)\n",
            "| Step | Title | Type | Agent | Scope |",
            "|------|-------|------|-------|-------|",
        ]
        for step in plan.steps:
            scope = ", ".join(step.file_scope) if step.file_scope else ""
            lines.append(
                f"| {step.step_id} | {step.title} | {step.type} | "
                f"{step.preferred_agent} | {scope} |"
            )
        lines.append("\n### Progress\n")
        self._append("\n".join(lines) + "\n")

    def append_step(
        self,
        step_id: str,
        title: str,
        agent: str,
        memory_note: str,
        verdict: str,
    ) -> None:
        icon = {
            "approved": "✓",
            "needs_revision": "⚠",
            "rejected": "✗",
        }.get(verdict, "·")
        lines = [
            f"\n#### [{icon}] {step_id}: {title}",
            f"- **Agent:** {agent}  **Time:** {_now()}  **Status:** {verdict}",
            f"- {memory_note}\n",
        ]
        self._append("\n".join(lines))

    def append_note(self, note: str) -> None:
        self._append(f"\n> **Note** ({_now()}): {note}\n")

    def complete_task(self, task_id: str) -> None:
        self._append(f"\n**Task `{task_id}` completed at {_now()}**\n\n---\n")

    def clear(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_replace(_HEADER)

    def _atomic_replace(self, content: str) -> None:
        """Durably replace the file without exposing a partially written header."""
        mode: int | None = None
        try:
            mode = self.path.stat().st_mode & 0o7777
        except FileNotFoundError:
            pass

        fd, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                _sync(stream)
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _append(self, text: str) -> None:
        with self._lock:
            self._ensure_initialized()
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                written = stream.write(text)
                if written != len(text):
                    raise OSError(
                        f"Incomplete write to memory file {self.path}: "
                        f"wrote {written} of {len(text)} characters"
                    )
                _sync(stream)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
