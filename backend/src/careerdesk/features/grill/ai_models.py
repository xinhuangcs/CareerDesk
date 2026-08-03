"""Strict, bounded judgement contract for immutable Grill items."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

FeedbackText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["meets", "partially_meets", "needs_work", "ungradable"]
    stuck: bool = Field(strict=True)
    strengths: list[FeedbackText] = Field(max_length=5)
    gaps: list[FeedbackText] = Field(max_length=5)
    next_step: FeedbackText
    follow_up: FeedbackText | None
