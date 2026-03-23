from __future__ import annotations
import anthropic
from genesis.agents.base import BaseAgent, AgentInfo


class ClaudeAgent(BaseAgent):
    def __init__(self, info: AgentInfo, api_key: str):
        super().__init__(info)
        self._client = anthropic.Anthropic(api_key=api_key)

    def chat(self, system: str, messages: list[dict]) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
        )
        return response.content[0].text
