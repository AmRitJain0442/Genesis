from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class Review(BaseModel):
    step_id: str
    verdict: Literal["approved", "needs_revision", "rejected"]
    quality_score: int = Field(ge=1, le=10)
    feedback: str
    memory_note: str
    should_retry: bool
    suggested_revision: str = ""
