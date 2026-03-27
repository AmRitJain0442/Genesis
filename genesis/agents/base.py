from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


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
