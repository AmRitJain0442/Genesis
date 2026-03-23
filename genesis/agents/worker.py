from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

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
    success: bool = True
    error: str = ""


class Worker:
    def __init__(self, agent: BaseAgent, memory_summary: str, work_dir: str = "."):
        self.agent = agent
        self.memory_summary = memory_summary
        self.work_dir = Path(work_dir)

    def execute(self, step: Step) -> WorkerResult:
        user_msg = self._build_message(step)
        try:
            raw = self.agent.chat(_SYSTEM, [{"role": "user", "content": user_msg}])
            return self._parse(raw, step)
        except Exception as e:
            logger.error("Worker error on %s: %s", step.step_id, e)
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

        for match in code_re.finditer(result_text):
            lang, filename, content = match.group(1), match.group(2), match.group(3)
            # Strip one leading newline that models typically add after the tag
            content = content.lstrip("\n")
            code_blocks.append(CodeBlock(language=lang, filename=filename, content=content))

            dest = self.work_dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            files_written.append(filename)
            logger.info("Wrote %s", filename)

        return WorkerResult(
            step_id=step.step_id,
            raw_response=raw,
            result_text=result_text,
            code_blocks=code_blocks,
            files_written=files_written,
            success=True,
        )
