"""HTTP contracts for interview generation."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...platform.locale import DEFAULT_OUTPUT_LOCALE, OutputLocale


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerateQuestionSetRequest(_Contract):
    edition: Literal["basic", "custom"]
    resume_id: int | None = Field(default=None, gt=0)
    application_id: int | None = Field(default=None, gt=0)
    client_command_id: UUID
    output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE
    refresh: bool = False

    @model_validator(mode="after")
    def validate_edition(self) -> "GenerateQuestionSetRequest":
        if self.edition == "basic":
            if self.resume_id is None or self.application_id is not None:
                raise ValueError("basic requires only resume_id")
        elif self.application_id is None or self.resume_id is not None:
            raise ValueError("custom requires only application_id")
        return self


class StartInterviewSessionRequest(_Contract):
    question_set_id: int = Field(gt=0)
    question_count: int = Field(default=10, gt=0)


class GenerationResponse(_Contract):
    status: Literal["processing", "ready", "error"]
    question_set_id: int | None = None
    code: str | None = None
    message: str | None = None


class ReadinessResponse(_Contract):
    resumes: list[dict]
    applications: dict
    question_sets: list[dict]
    model_configured: bool
    selection: dict | None = None


class QuestionSetStatusResponse(_Contract):
    status: str | None = None
    id: int | None = None
    kind: str | None = None
    edition: str | None = None
    resume_id: int | None = None
    application_id: int | None = None
    state: str | None = None
    stage: str | None = None
    safe_error_code: str | None = None
    context_label: str | None = None
    archived_at: str | None = None
    question_count: int | None = None
    unpracticed_count: int | None = None
    created_time: str | None = None
    updated_time: str | None = None
    content_locale: OutputLocale | None = None
    currentness: str | None = None
    code: str | None = None


class QuestionSetArchiveResponse(_Contract):
    status: str
