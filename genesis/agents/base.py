from __future__ import annotations
import os
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


def terminate_process_tree(proc: "subprocess.Popen") -> None:
    """Best-effort kill of a child process and all of its descendants.

    Killing only the immediate child can leave grandchildren alive — e.g. the
    node/native process spawned behind a ``.cmd`` shim on Windows — and those
    grandchildren keep the stdout pipe open, blocking readers indefinitely. On
    Windows we use ``taskkill /T`` to take down the whole tree; on POSIX we kill
    the process group (requires ``start_new_session=True`` at spawn time).
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
            if result.returncode != 0 and proc.poll() is None:
                proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
    except Exception:
        # Last-ditch fallback — kill at least the direct child.
        try:
            proc.kill()
        except Exception:
            pass


@dataclass
class AgentInfo:
    name: str
    provider: str   # "claude-cli" | "codex-cli" | "chatgpt-browser"
    model: str
    max_tokens: int


class BaseAgent(ABC):
    def __init__(self, info: AgentInfo):
        self.info = info
        self.name = info.name
        self.provider = info.provider
        self.model = info.model
        self.max_tokens = info.max_tokens

    @abstractmethod
    def chat(
        self,
        system: str,
        messages: list[dict],
        output_callback: Callable[[str], None] | None = None,
    ) -> str:
        """Send a conversation to the model and return the text response.

        If output_callback is provided, implementations should stream
        partial output to it as it arrives.
        """
        ...

    def ping(self) -> bool:
        """Check that the agent is reachable. Returns True on success."""
        try:
            out = self.chat(
                "You are a test assistant.",
                [{"role": "user", "content": "Reply with the single word: ok"}],
            )
            return bool(out)
        except Exception:
            return False
