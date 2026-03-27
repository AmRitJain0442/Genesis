"""
CodexCLIAgent — drives the OpenAI Codex CLI (`codex`) as a subprocess.

Authentication is handled by Codex itself (OAuth via `codex login` or your
existing ChatGPT Pro session). No API key required.

Two modes:
  chat()         — pure text response (for orchestrator JSON prompts)
  execute_task() — autonomous execution (writes files, runs commands, for workers)
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
import logging
from pathlib import Path
from typing import Callable

from genesis.agents.base import BaseAgent, AgentInfo

logger = logging.getLogger(__name__)


def find_codex_binary() -> str | None:
    """Return the path to the `codex` binary, or None if not found."""
    return shutil.which("codex")


class CodexCLIAgent(BaseAgent):
    """
    Wraps the Codex CLI for two use-cases:

    1. chat() — runs `codex exec --ephemeral` and captures the last message.
       Used by the orchestrator for planning / reviewing (expects JSON back).

    2. execute_task() — runs `codex exec` with workspace-write sandbox so
       Codex can create/modify files directly in the project directory.
       Used by CodexWorker. Returns the last agent message (a summary of
       what was done).

    Pass codex_home to isolate a specific ChatGPT Pro account:
        agent = CodexCLIAgent(..., codex_home="C:/Users/amrit/.codex-account2")
    This sets CODEX_HOME when invoking the CLI, pointing it at a different
    auth.json so multiple accounts can be used simultaneously.
    """

    def __init__(
        self,
        info: AgentInfo,
        command: str = "codex",
        timeout: int = 600,
        work_dir: str = ".",
        codex_home: str = "",   # empty = use system default (~/.codex)
    ):
        super().__init__(info)
        self.command = command
        self.timeout = timeout
        self.work_dir = str(Path(work_dir).resolve())
        # Normalise codex_home to OS path (config may use forward slashes on Windows)
        self.codex_home = str(Path(codex_home)) if codex_home else ""

    # ── BaseAgent interface ────────────────────────────────────────────────

    def chat(self, system: str, messages: list[dict],
             output_callback: Callable[[str], None] | None = None) -> str:
        """
        Pure text/JSON exchange — ephemeral, no file writes.
        Used for orchestrator planning and review calls.
        """
        prompt = self._build_prompt(system, messages)
        return self._run(prompt, allow_writes=False, output_callback=output_callback)

    def ping(self) -> bool:
        try:
            result = self._run("Reply with one word: OK", allow_writes=False)
            return "OK" in result.upper()
        except Exception as e:
            logger.warning("codex ping failed: %s", e)
            return False

    # ── Worker-specific method ─────────────────────────────────────────────

    def execute_task(self, prompt: str,
                     output_callback: Callable[[str], None] | None = None) -> str:
        """
        Autonomous execution — Codex can write files and run shell commands
        inside the workspace. Returns its final summary message.
        """
        return self._run(prompt, allow_writes=True, output_callback=output_callback)

    # ── Internals ──────────────────────────────────────────────────────────

    def _build_prompt(self, system: str, messages: list[dict]) -> str:
        parts = [system]
        for msg in messages:
            content = msg.get("content", "")
            if content:
                parts.append(content)
        return "\n\n---\n\n".join(parts)

    def _run(self, prompt: str, allow_writes: bool,
             output_callback: Callable[[str], None] | None = None) -> str:
        """
        Run `codex exec` with the given prompt via stdin.
        Returns the last agent message text.
        """
        if output_callback is not None:
            return self._run_streaming(prompt, allow_writes, output_callback)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            output_file = tmp.name

        # Escape single quotes in work_dir so they don't break the -c config string
        safe_work_dir = self.work_dir.replace("'", "\\'")

        cmd = [
            self.command, "exec",
            "--full-auto",
            "-C", self.work_dir,
            "-o", output_file,
            "-",  # read prompt from stdin
        ]

        if not allow_writes:
            cmd.append("--ephemeral")

        # Trust the working directory so Codex doesn't prompt for confirmation
        # when running in a repo it hasn't seen before
        cmd += ["-c", f"projects.'{safe_work_dir}'.trust_level=trusted"]

        # Only override model if explicitly set (let Codex pick its default otherwise)
        if self.model and self.model not in ("auto", "default"):
            cmd += ["--model", self.model]

        if allow_writes:
            cmd += ["--sandbox", "workspace-write"]
        else:
            cmd += ["--sandbox", "read-only"]

        # On Windows, .cmd/.bat scripts must be invoked via cmd.exe
        if os.name == "nt" and cmd[0].lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c"] + cmd

        # Build env — inherit current env, add CODEX_HOME if this agent has one
        env = os.environ.copy()
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home

        logger.debug("codex cmd: %s (home=%s)", " ".join(cmd[:6]), self.codex_home or "default")

        output_path = Path(output_file)
        try:
            try:
                result = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    encoding="utf-8",
                    env=env,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"Codex timed out after {self.timeout}s")

            last_message = ""
            if output_path.exists():
                last_message = output_path.read_text(encoding="utf-8").strip()

            if result.returncode != 0 and not last_message:
                stderr = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"Codex exited {result.returncode}: {stderr[:400]}")

            return last_message or result.stdout.strip()
        finally:
            output_path.unlink(missing_ok=True)

    def _run_streaming(
        self,
        prompt: str,
        allow_writes: bool,
        output_callback: Callable[[str], None],
    ) -> str:
        """
        Run `codex exec --json` and stream JSONL events to output_callback.
        Returns the last agent_message content as the final result.
        """
        safe_work_dir = self.work_dir.replace("'", "\\'")

        cmd = [
            self.command, "exec",
            "--full-auto",
            "--json",
            "-C", self.work_dir,
            "-",
        ]

        if not allow_writes:
            cmd.append("--ephemeral")

        cmd += ["-c", f"projects.'{safe_work_dir}'.trust_level=trusted"]

        if self.model and self.model not in ("auto", "default"):
            cmd += ["--model", self.model]

        if allow_writes:
            cmd += ["--sandbox", "workspace-write"]
        else:
            cmd += ["--sandbox", "read-only"]

        if os.name == "nt" and cmd[0].lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c"] + cmd

        env = os.environ.copy()
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home

        logger.debug("codex streaming cmd: %s (home=%s)", " ".join(cmd[:6]), self.codex_home or "default")

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )

        # Write prompt and close stdin — guard against broken pipe on early exit
        try:
            try:
                proc.stdin.write(prompt)
            finally:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        last_message = ""
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict):
                    continue

                event_type = event.get("type", "")

                if event_type == "item.completed":
                    item = event.get("item")
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")

                    if item_type == "agent_message":
                        content = item.get("content") or ""
                        if isinstance(content, str) and content:
                            last_message = content
                            output_callback(content)

                    elif item_type == "command_execution":
                        cmd_str = item.get("cmd") or ""
                        agg_out = item.get("aggregated_output") or ""
                        exit_code = item.get("exit_code", 0)
                        output_callback(f"[bold yellow]$ {cmd_str}[/bold yellow]")
                        if isinstance(agg_out, str) and agg_out.strip():
                            for out_line in agg_out.strip().splitlines()[:5]:
                                output_callback(f"  [dim]{out_line}[/dim]")
                        if exit_code != 0:
                            output_callback(f"  [red]exit {exit_code}[/red]")

                    elif item_type == "file_change":
                        path = item.get("path") or ""
                        mode = item.get("mode") or ""
                        icon = "+" if mode == "create" else "~"
                        output_callback(f"[green]{icon} {path}[/green]")

                elif event_type == "turn.completed":
                    usage = event.get("usage") or {}
                    inp = usage.get("input_tokens", 0)
                    cached = usage.get("cached_input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    output_callback(
                        f"[dim]Tokens: in={inp} (cached={cached}) out={out}[/dim]"
                    )
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        if proc.returncode != 0 and not last_message:
            stderr = proc.stderr.read().strip()
            raise RuntimeError(f"Codex exited {proc.returncode}: {stderr[:400]}")

        return last_message
