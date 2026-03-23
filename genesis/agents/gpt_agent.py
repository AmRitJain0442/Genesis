from __future__ import annotations
import openai
from genesis.agents.base import BaseAgent, AgentInfo


class GPTAgent(BaseAgent):
    def __init__(self, info: AgentInfo, api_key: str):
        super().__init__(info)
        self._client = openai.OpenAI(api_key=api_key)

    def chat(self, system: str, messages: list[dict]) -> str:
        # OpenAI uses system as the first message
        full_messages = [{"role": "system", "content": system}] + messages
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=full_messages,
        )
        return response.choices[0].message.content
