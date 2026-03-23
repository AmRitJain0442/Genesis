from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class Step(BaseModel):
    step_id: str
    title: str
    description: str
    type: Literal["code", "docs", "review", "research", "test", "config", "refactor"]
    preferred_agent: Literal["claude-worker", "gpt-worker", "any"] = "any"
    depends_on: list[str] = Field(default_factory=list)
    expected_output: str
    context_hint: str = ""


class Plan(BaseModel):
    task_id: str
    task_summary: str
    estimated_steps: int
    steps: list[Step]
