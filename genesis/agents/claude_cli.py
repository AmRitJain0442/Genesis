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
from typing import Callable
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
                    "preferred_agent":{"type": "string", "default": "any"},
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

    def chat(self, system: str, messages: list[dict],
             output_callback: Callable[[str], None] | None = None) -> str:
        prompt = self._build_prompt(system, messages)
        if output_callback is not None:
            return self._call_streaming(prompt, output_callback)
        return self._call(prompt)

    # ── Specialised methods for orchestrator use ───────────────────────────

    def chat_plan(self, system: str, messages: list[dict]) -> str:
        """Generate a plan using streaming to avoid output truncation on long JSON."""
        prompt = self._build_prompt(system, messages)
        # --json-schema (batch) truncates long plans; streaming reads every token
        return self._call_streaming(prompt, lambda _: None)

    def chat_review(self, system: str, messages: list[dict]) -> str:
        """Generate a review using streaming to avoid output truncation."""
        prompt = self._build_prompt(system, messages)
        return self._call_streaming(prompt, lambda _: None)

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

        # --json-schema puts the validated object in "structured_output", not "result"
        if json_schema:
            structured = envelope.get("structured_output")
            if structured is not None:
                # Already a JSON string (CLI pre-encoded it) — return as-is to avoid
                # double-encoding which breaks _extract_json (escaped quotes at char 1)
                if isinstance(structured, str):
                    return structured
                return json.dumps(structured)

        raw_result = envelope.get("result", "")
        return raw_result if isinstance(raw_result, str) else json.dumps(raw_result)

    def ping(self) -> bool:
        try:
            out = self._call("Reply with exactly one word: OK")
            return "OK" in out.upper()
        except Exception as e:
            logger.warning("ping failed: %s", e)
            return False

    def _call_streaming(self, prompt: str, output_callback: Callable[[str], None]) -> str:
        """
        Run Claude with --output-format stream-json --verbose, emit live events
        to output_callback, and return the final result text.
        """
        cmd = [
            self.command,
            "--print",
            "--model", self.model,
            "--output-format", "stream-json",
            "--verbose",
        ]

        logger.debug("Claude streaming cmd: %s", " ".join(cmd[:5]))

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        result_text = ""
        accumulated_text: list[str] = []
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        btype = block.get("type", "")
                        if btype == "thinking":
                            thinking = block.get("thinking", "")
                            if thinking:
                                preview = thinking[:120].replace("\n", " ")
                                suffix = "..." if len(thinking) > 120 else ""
                                output_callback(f"[dim]Thinking: {preview}{suffix}[/dim]")
                        elif btype == "text":
                            text = block.get("text", "")
                            if text:
                                accumulated_text.append(text)
                                output_callback(text)

                elif event_type == "result":
                    usage = event.get("usage", {})
                    cost = event.get("total_cost_usd", 0.0)
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    output_callback(
                        f"[dim]Tokens: in={inp} out={out} | ${cost:.4f}[/dim]"
                    )
                    result_text = event.get("result") or ""
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        # Always prefer the joined text blocks — they are the raw model output and
        # avoid any post-processing the CLI applies to the result field.
        # Fall back to result field if no text blocks were collected.
        final_text = "".join(accumulated_text) or result_text

        if proc.returncode != 0 and not final_text:
            err = proc.stderr.read().strip()
            raise RuntimeError(f"Claude CLI exited {proc.returncode}: {err[:400]}")

        logger.debug("_call_streaming: accumulated=%d chars, result=%d chars",
                     len("".join(accumulated_text)), len(result_text))
        return final_text
