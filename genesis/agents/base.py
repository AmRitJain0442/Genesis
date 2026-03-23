from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AgentInfo:
    name: str
    provider: str   # "claude" | "gpt"
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
    def chat(self, system: str, messages: list[dict]) -> str:
        """Send a conversation to the model and return the text response."""
        ...

    def ping(self) -> bool:
        """Check that the agent is reachable. Returns True on success."""
        try:
            self.chat("You are a test assistant.", [{"role": "user", "content": "Reply with the single word: ok"}])
            return True
        except Exception:
            return False
