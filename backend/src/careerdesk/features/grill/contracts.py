"""Exact HTTP response contracts for the question-set Grill state machine."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProgressDTO(_Contract):
    answered: int = Field(ge=0)
    total: int = Field(ge=0)


class QuestionDTO(_Contract):
    id: int = Field(gt=0)  # canonical identity is grill_session_items.id
    text: str
    category: str
    channel: Literal["interview", "written"]
    response_format: str
    difficulty: str
    primary_competency: str
    secondary_tags: list[str]


class GrillFlowResponse(_Contract):
    status: Literal["ok", "error", "processing", "finished", "suspended"]
    session_id: int | None = None
    question: QuestionDTO | None = None
    follow_up: str | None = None
    ack: str | None = None
    progress: ProgressDTO | None = None
    summary: dict | None = None
    code: str | None = None
    message: str | None = None


class SessionDTO(_Contract):
    id: int
    question_set_id: int
    kind: Literal["generated", "library_snapshot"]
    edition: Literal["basic", "custom"] | None
    context_label: str
    state: Literal["active", "suspended", "finished"]
    answered: int
    total: int
    started_time: str
    ended_time: str | None


class GrillSessionsResponse(_Contract):
    items: list[SessionDTO]


class ReplayAnswerDTO(_Contract):
    session_item_id: int
    text: str
    category: str
    verdict: Literal["meets", "partially_meets", "needs_work", "ungradable", "skipped"]
    stuck: bool
    feedback: dict
    answer_guide: dict
    primary_competency: str
    transcript: list[dict]


class GrillReplayResponse(_Contract):
    status: Literal["ok", "error", "processing"]
    session_id: int | None = None
    context_label: str | None = None
    kind: str | None = None
    edition: str | None = None
    answers: list[ReplayAnswerDTO] | None = None
    summary: dict | None = None
    code: str | None = None
    message: str | None = None


class GrillDeleteResponse(_Contract):
    status: Literal["ok", "error"]
    message: str | None = None
