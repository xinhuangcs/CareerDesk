"""Whole-Review undo proposal and receipt contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ...applications.public import ApplicationNextAction, ApplicationStage
from ..ai_models import CompanyText, PositionText, StepText, SummaryText

REVIEW_UNDO_CONTRACT_VERSION = 1
MAX_CONTENT_PREVIEW_CHARS = 2_000
MAX_UNDO_TIMELINE_ENTRIES = 100
MAX_UNDO_STATUS_LOGS = 100
MAX_UNDO_QUESTIONS = 50
MAX_OPERATION_ERROR_CHARS = 500

BoundedTimestamp = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
ContentPreview = Annotated[str, StringConstraints(max_length=MAX_CONTENT_PREVIEW_CHARS)]
QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
OperationErrorText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_OPERATION_ERROR_CHARS),
]


class ReviewUndoTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    journal_id: int = Field(gt=0)
    expected_revision: int = Field(ge=0)
    company: CompanyText
    position: PositionText
    content_preview: ContentPreview
    content_truncated: bool
    review_created_time: BoundedTimestamp


class ReviewUndoTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    step: StepText | None = None
    occurred_date: str | None = Field(default=None, max_length=10)
    outcome: Literal["passed", "failed", "cancelled"] | None = None
    summary: SummaryText | None = None
    from_stage: ApplicationStage
    from_step: StepText | None = None
    to_stage: ApplicationStage
    to_step: StepText | None = None


class ReviewUndoQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    text: QuestionText


class ReviewUndoApplicationProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    stage: ApplicationStage
    current_step: StepText | None = None
    current_state_entry_id: int | None = Field(default=None, gt=0)
    next_action: ApplicationNextAction | None = None
    paused_from_stage: ApplicationStage | None = None
    pause_reason: str | None = Field(default=None, max_length=1_000)
    channel: str | None = Field(default=None, max_length=100)
    applied_date: str | None = Field(default=None, max_length=10)
    revision: int = Field(ge=0)


class ReviewUndoApplication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int | None = Field(default=None, gt=0)
    company: CompanyText
    position: PositionText
    record_exists: bool
    record_retained: bool
    expected: ReviewUndoApplicationProjection | None = None
    replacement: ReviewUndoApplicationProjection | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "ReviewUndoApplication":
        if self.record_exists and (self.id is None or self.expected is None):
            raise ValueError("existing application requires identity and expected projection")
        if self.record_retained and self.replacement is None:
            raise ValueError("retained application requires replacement projection")
        if not self.record_exists and any(value is not None for value in (
            self.id, self.expected, self.replacement,
        )):
            raise ValueError("missing application cannot expose a projection")
        if not self.record_retained and self.replacement is not None:
            raise ValueError("removed application cannot expose a replacement")
        return self


class ReviewUndoEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeline_entries: list[ReviewUndoTimelineEntry] = Field(
        default_factory=list,
        max_length=MAX_UNDO_TIMELINE_ENTRIES,
    )
    status_logs_removed: int = Field(ge=0, le=MAX_UNDO_STATUS_LOGS)
    questions_archived: list[ReviewUndoQuestion] = Field(
        default_factory=list,
        max_length=MAX_UNDO_QUESTIONS,
    )
    application: ReviewUndoApplication


class ReviewUndoProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["review_undo"]
    contract_version: Literal[REVIEW_UNDO_CONTRACT_VERSION]
    dependency_fingerprint: Fingerprint
    target: ReviewUndoTarget
    effect: ReviewUndoEffect


class ReviewUndoRemoved(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeline_entries: int = Field(ge=0, le=MAX_UNDO_TIMELINE_ENTRIES)
    status_logs: int = Field(ge=0, le=MAX_UNDO_STATUS_LOGS)
    questions_archived: int = Field(ge=0, le=MAX_UNDO_QUESTIONS)


class ReviewUndoResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["ok"]
    target_revision: int = Field(gt=0)
    application_id: int | None = Field(default=None, gt=0)
    application_stage: ApplicationStage | None = None
    removed: ReviewUndoRemoved


class ReviewOperationError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,100}$")]
    message: OperationErrorText


class ReviewOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: Annotated[
        str,
        StringConstraints(pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )),
    ]
    state: Literal["pending", "completed", "rejected", "stale"]
    created_time: BoundedTimestamp
    target: ReviewUndoTarget
    effect: ReviewUndoEffect
    result: ReviewUndoResult | None

    @model_validator(mode="after")
    def validate_result_state(self) -> "ReviewOperationDTO":
        if (self.state == "completed") != (self.result is not None):
            raise ValueError("only completed operations may expose a result")
        return self
