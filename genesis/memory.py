from __future__ import annotations
from pathlib import Path
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.schemas.plan import Plan, Step

_HEADER = """\
# GENESIS MEMORY
*AI Orchestration System — Shared Project Memory*

---

"""


class MemoryManager:
    def __init__(self, file_path: str):
        self.path = Path(file_path)
        if not self.path.exists():
            self.path.write_text(_HEADER, encoding="utf-8")

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def get_summary(self, max_chars: int = 6000) -> str:
        """Return the last `max_chars` characters, trimmed to a paragraph boundary."""
        content = self.read()
        if len(content) <= max_chars:
            return content
        truncated = content[-max_chars:]
        # Prefer a paragraph break (double newline) to avoid splitting mid-section
        idx = truncated.find("\n\n")
        if idx > 0:
            truncated = truncated[idx + 2:]
        else:
            idx = truncated.find("\n")
            if idx > 0:
                truncated = truncated[idx + 1:]
        return "[...earlier context truncated...]\n\n" + truncated

    def append_plan(self, plan: Plan) -> None:
        ts = _now()
        lines = [
            f"\n## Task: {plan.task_summary}",
            f"*Started: {ts}* · Task ID: `{plan.task_id}`\n",
            f"### Plan ({plan.estimated_steps} steps)\n",
            "| Step | Title | Type | Agent |",
            "|------|-------|------|-------|",
        ]
        for s in plan.steps:
            lines.append(f"| {s.step_id} | {s.title} | {s.type} | {s.preferred_agent} |")
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
        icon = {"approved": "✓", "needs_revision": "⚠", "rejected": "✗"}.get(verdict, "·")
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
        self.path.write_text(_HEADER, encoding="utf-8")

    def _append(self, text: str) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            import logging
            logging.getLogger(__name__).warning(
                "Could not write to memory file %s: %s", self.path, e
            )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
