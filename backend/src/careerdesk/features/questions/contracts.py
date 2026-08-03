"""HTTP contracts for the question catalogue."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionDTO(_Contract):
    id: int = Field(gt=0)
    text: str
    source: Literal["generated"]
    company: str | None
    source_step: str | None
    asked_date: str | None
    quality_flag: Literal["good", "bad"] | None
    category: str
    channel: Literal["interview", "written"]
    response_format: str
    primary_competency: str
    secondary_tags: list[str]
    answer_guide: dict[str, Any] | None
    answer_verified: bool
    question_set_id: int = Field(gt=0)
    immutable_revision: int
    knowledge_points: list[dict[str, Any]]
    weakest_box: int | None
    edition: Literal["basic", "custom"]
    context_label: str


class QuestionsResponse(_Contract):
    items: list[QuestionDTO]
    weak_points: list[dict[str, Any]]
    has_more: bool = False
    next_offset: int | None = None


class CommandResponse(_Contract):
    status: Literal["ok", "error"]
    message: str | None = None


class CompetencyProgressResponse(_Contract):
    aggregate: list[dict[str, Any]]
    scopes: list[dict[str, Any]]
