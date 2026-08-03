"""Resumes HTTP response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeLineDTO(_Contract):
    text: str
    knowledge_points: list[str]


class ResumeDTO(_Contract):
    id: int = Field(gt=0)
    name: str
    binding: Literal["family", "application"]
    application_id: int | None
    application_company: str | None
    application_position: str | None
    archived: bool
    content_hash: str
    character_count: int = Field(ge=0)
    annotation_status: Literal["pending", "ready", "failed"]
    updated_time: str


class ResumesResponse(_Contract):
    items: list[ResumeDTO]


class ResumeTextResponse(_Contract):
    id: int = Field(gt=0)
    name: str
    content_text: str
    content_hash: str = Field(min_length=64, max_length=64)
    character_count: int = Field(ge=0)
    updated_time: str


class ResumeMutationResponse(_Contract):
    status: Literal["ok", "processing", "error", "stale"]
    message: str | None = None
    resume_id: int | None = None
    family: str | None = None
    line_count: int | None = Field(default=None, ge=0)
    cleanup_warning: str | None = None
    job_id: str | None = None


class ResumeJobDTO(_Contract):
    job_id: str
    operation: Literal["create", "update"]
    target_resume_id: int | None
    name: str
    state: Literal["processing", "completed", "failed"]
    stage: Literal["queued", "extracting", "parsing", "saving", "completed", "failed"]
    message: str | None
    resume_id: int | None
    created_time: str
    updated_time: str


class ResumeJobsResponse(_Contract):
    items: list[ResumeJobDTO]


class ResumeJobDismissResponse(_Contract):
    status: Literal["ok"]
    dismissed: bool


class ResumeDeleteResponse(_Contract):
    status: Literal["ok", "error"]
    message: str | None = None
    cleanup_warning: str | None = None
