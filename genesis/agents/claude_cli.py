"""
ClaudeCodeCLIAgent — drives the Claude Code CLI (`claude`) as a subprocess.

Authentication is handled by Claude Code itself (OAuth via `claude login`).
No API keys required.

Claude Code docs on non-interactive use:
  claude --print --model <model> --output-format json
  Accepts a prompt via -p "<text>" or via stdin when -p is omitted.
"""
from __future__ import annotations
import json
import subprocess
import logging
import shutil
from genesis.agents.base import BaseAgent, AgentInfo

logger = logging.getLogger(__name__)

# Passed to --json-schema when we want the orchestrator to return a Plan
_PLAN_SCHEMA = json.dumps({
    "type": "object",
    "required": ["task_id", "task_summary", "estimated_steps", "steps"],
    "properties": {
        "task_id":        {"type": "string"},
        "task_summary":   {"type": "string"},
        "estimated_steps":{"type": "integer"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["step_id", "title", "description", "type",
                             "preferred_agent", "depends_on", "expected_output"],
                "properties": {
                    "step_id":        {"type": "string"},
                    "title":          {"type": "string"},
                    "description":    {"type": "string"},
                    "type":           {"type": "string"},
                    "preferred_agent":{"type": "string"},
                    "depends_on":     {"type": "array", "items": {"type": "string"}},
                    "expected_output":{"type": "string"},
                    "context_hint":   {"type": "string"},
                },
            },
        },
    },
})

_REVIEW_SCHEMA = json.dumps({
    "type": "object",
    "required": ["step_id", "verdict", "quality_score", "feedback",
                 "memory_note", "should_retry"],
    "properties": {
        "step_id":           {"type": "string"},
        "verdict":           {"type": "string", "enum": ["approved", "needs_revision", "rejected"]},
        "quality_score":     {"type": "integer", "minimum": 1, "maximum": 10},
        "feedback":          {"type": "string"},
        "memory_note":       {"type": "string"},
        "should_retry":      {"type": "boolean"},
        "suggested_revision":{"type": "string"},
    },
})


def find_claude_binary() -> str | None:
    """Return the path to the `claude` binary, or None if not found."""
    return shutil.which("claude")


class ClaudeCodeCLIAgent(BaseAgent):
    """
    Calls Claude Code CLI non-interactively.

    Uses stdin for the prompt and --output-format json so we always get a
    clean `result` field back, regardless of prompt length.
    """

    def __init__(
        self,
        info: AgentInfo,
        command: str = "claude",
        timeout: int = 300,
    ):
        super().__init__(info)
        self.command = command
        self.timeout = timeout

    # ── Core chat method ───────────────────────────────────────────────────

    def chat(self, system: str, messages: list[dict]) -> str:
        prompt = self._build_prompt(system, messages)
        return self._call(prompt)

    # ── Specialised methods for orchestrator use ───────────────────────────

    def chat_plan(self, system: str, messages: list[dict]) -> str:
        """Chat with --json-schema for Plan output. Falls back to plain chat."""
        prompt = self._build_prompt(system, messages)
        return self._call(prompt, json_schema=_PLAN_SCHEMA)

    def chat_review(self, system: str, messages: list[dict]) -> str:
        """Chat with --json-schema for Review output. Falls back to plain chat."""
        prompt = self._build_prompt(system, messages)
        return self._call(prompt, json_schema=_REVIEW_SCHEMA)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _build_prompt(self, system: str, messages: list[dict]) -> str:
        parts = [system]
        for msg in messages:
            content = msg.get("content", "")
            if content:
                parts.append(content)
        return "\n\n---\n\n".join(parts)

    def _call(self, prompt: str, json_schema: str | None = None) -> str:
        cmd = [
            self.command,
            "--print",
            "--model", self.model,
            "--output-format", "json",
        ]
        if json_schema:
            cmd += ["--json-schema", json_schema]

        logger.debug("Calling: %s (prompt %d chars)", " ".join(cmd[:4]), len(prompt))

        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            encoding="utf-8",
        )

        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Claude CLI exited {result.returncode}: {err[:400]}")

        # Parse the JSON envelope
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            # If JSON parsing fails, return raw stdout (shouldn't happen with --output-format json)
            return result.stdout.strip()

        if envelope.get("is_error"):
            raise RuntimeError(f"Claude CLI error response: {envelope}")

        raw_result = envelope.get("result", "")

        # If we used --json-schema, the result IS the JSON object
        # Return it serialised so the caller can parse it uniformly
        if json_schema and isinstance(raw_result, dict):
            return json.dumps(raw_result)

        return raw_result if isinstance(raw_result, str) else json.dumps(raw_result)

    def ping(self) -> bool:
        try:
            out = self._call("Reply with exactly one word: OK")
            return "OK" in out.upper()
        except Exception as e:
            logger.warning("ping failed: %s", e)
            return False
