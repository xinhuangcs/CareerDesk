"""Review timeline-entry edit operation contracts."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ...applications.public import ApplicationStage
from ..ai_models import CompanyText, PositionText, StepText, SummaryText

REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION = 1
MAX_EDIT_OCCURRENCES = 50
MAX_EDIT_STATUS_LOGS = 1

EditField = Literal["step", "occurred_date", "outcome", "summary"]
UUIDText = Annotated[
    str,
    StringConstraints(pattern=(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}$"
    )),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BoundedTimestamp = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
ISODateText = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Outcome = Literal["passed", "failed", "cancelled"]


def _validate_real_iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("date must be a real ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError("date must be a canonical ISO date")
    return value


class ReviewTimelineEntryEditChanges(BaseModel):
    """Patch fields; ``model_fields_set`` preserves explicit null clears."""

    model_config = ConfigDict(extra="forbid", strict=True)

    step: StepText | None = None
    occurred_date: ISODateText | None = None
    outcome: Outcome | None = None
    summary: SummaryText | None = None

    @field_validator("occurred_date")
    @classmethod
    def validate_real_date(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)

    @model_validator(mode="after")
    def require_change(self) -> "ReviewTimelineEntryEditChanges":
        if not self.model_fields_set:
            raise ValueError("timeline entry edit requires at least one field")
        return self


class ReviewTimelineEntryEditCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    company: CompanyText | None = None
    position: PositionText | None = None
    changes: ReviewTimelineEntryEditChanges


class ReviewTimelineEntryEditTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    journal_id: int = Field(gt=0)
    journal_created_time: BoundedTimestamp
    application_id: int = Field(gt=0)
    application_created_time: BoundedTimestamp
    timeline_entry_id: int = Field(gt=0)
    timeline_entry_created_time: BoundedTimestamp
    company: CompanyText
    position: PositionText


class ReviewTimelineEntryProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    step: StepText | None = None
    occurred_date: ISODateText | None = None
    outcome: Outcome | None = None
    summary: SummaryText | None = None
    from_stage: ApplicationStage
    from_step: StepText | None = None
    to_stage: ApplicationStage
    to_step: StepText | None = None
    journal_revision: int = Field(ge=0)

    @field_validator("occurred_date")
    @classmethod
    def validate_real_date(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)


class ReviewTimelineEntryApplicationProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    stage: ApplicationStage
    current_step: StepText | None = None
    current_state_entry_id: int | None = Field(default=None, gt=0)
    revision: int = Field(ge=0)


class ReviewTimelineEntryOccurrenceEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question_id: int = Field(gt=0)
    application_id: int = Field(gt=0)
    company: CompanyText
    before_source_step: StepText | None = None
    after_source_step: StepText | None = None
    before_asked_date: ISODateText | None = None
    after_asked_date: ISODateText | None = None

    @field_validator("before_asked_date", "after_asked_date")
    @classmethod
    def validate_real_dates(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)


class ReviewTimelineEntryStatusLogEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    created_time: BoundedTimestamp
    before_log_date: ISODateText
    after_log_date: ISODateText

    @field_validator("before_log_date", "after_log_date")
    @classmethod
    def validate_real_dates(cls, value: str) -> str:
        validated = _validate_real_iso_date(value)
        if validated is None:  # pragma: no cover
            raise ValueError("status log date is required")
        return validated


class ReviewTimelineEntryEditEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    changed_fields: list[EditField] = Field(min_length=1, max_length=4)
    occurrences: list[ReviewTimelineEntryOccurrenceEffect] = Field(
        default_factory=list,
        max_length=MAX_EDIT_OCCURRENCES,
    )
    status_logs: list[ReviewTimelineEntryStatusLogEffect] = Field(
        default_factory=list,
        max_length=MAX_EDIT_STATUS_LOGS,
    )
    application_before: ReviewTimelineEntryApplicationProjection
    application_final: ReviewTimelineEntryApplicationProjection
    before_dependency_fingerprint: Digest
    final_dependency_fingerprint: Digest
    questions_untouched: Literal[True]
    knowledge_untouched: Literal[True]


class ReviewTimelineEntryEditProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["review_timeline_entry_edit"]
    contract_version: Literal[REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION]
    client_turn_id: UUIDText
    request_digest: Digest
    target: ReviewTimelineEntryEditTarget
    before: ReviewTimelineEntryProjection
    final: ReviewTimelineEntryProjection
    effect: ReviewTimelineEntryEditEffect

    @model_validator(mode="after")
    def validate_projection(self) -> "ReviewTimelineEntryEditProposal":
        if self.final.journal_revision != self.before.journal_revision + 1:
            raise ValueError("review edit revision must advance exactly once")
        fields = set(self.effect.changed_fields)
        for field in ("step", "occurred_date", "outcome", "summary"):
            if (getattr(self.before, field) != getattr(self.final, field)) != (field in fields):
                raise ValueError("review edit fields disagree with projections")
        return self


class ReviewTimelineEntryEditApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["ok"]
    journal_id: int = Field(gt=0)
    timeline_entry_id: int = Field(gt=0)
    application_id: int = Field(gt=0)
    target_revision: int = Field(gt=0)
    application_revision: int = Field(ge=1)
    timeline_entries_updated: Literal[1]
    occurrences_updated: int = Field(ge=0, le=MAX_EDIT_OCCURRENCES)
    status_logs_updated: int = Field(ge=0, le=MAX_EDIT_STATUS_LOGS)
    application_updated: Literal[True]


class ReviewTimelineEntryEditUndoResult(ReviewTimelineEntryEditApplyResult):
    pass


class ReviewTimelineEntryEditReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    apply: ReviewTimelineEntryEditApplyResult
    undo: ReviewTimelineEntryEditUndoResult | None = None


ReviewTimelineEntryEditUndoBlockReason = Literal[
    "target_missing",
    "target_changed",
    "provenance_changed",
    "operation_invalid",
    "already_undone",
]
ReviewTimelineEntryEditUndoErrorCode = Literal[
    "operation_not_found",
    "operation_invalid",
    "target_missing",
    "target_changed",
    "provenance_changed",
]


class ReviewTimelineEntryEditUndoCommandError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: ReviewTimelineEntryEditUndoErrorCode
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
    ]


class ReviewTimelineEntryEditUndoCommandStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command_id: UUIDText
    operation_id: UUIDText | None = None
    state: Literal["absent", "completed", "rejected"]
    terminal: bool
    error: ReviewTimelineEntryEditUndoCommandError | None = None
    finished_time: BoundedTimestamp | None = None

    @model_validator(mode="after")
    def validate_terminal_receipt(self) -> "ReviewTimelineEntryEditUndoCommandStatus":
        if self.state == "absent":
            if self.terminal or any(value is not None for value in (
                self.operation_id, self.error, self.finished_time,
            )):
                raise ValueError("absent undo command cannot expose a receipt")
        elif self.state == "completed":
            if (
                not self.terminal
                or self.operation_id is None
                or self.error is not None
                or self.finished_time is None
            ):
                raise ValueError("completed undo command receipt is inconsistent")
        elif (
            not self.terminal
            or self.operation_id is None
            or self.error is None
            or self.finished_time is None
        ):
            raise ValueError("rejected undo command receipt is inconsistent")
        return self


class ReviewTimelineEntryEditOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: UUIDText
    operation_type: Literal["review_timeline_entry_edit"]
    contract_version: Literal[REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION]
    state: Literal["completed", "undone", "stale"]
    created_time: BoundedTimestamp
    client_turn_id: UUIDText
    request_digest: Digest
    target: ReviewTimelineEntryEditTarget
    before: ReviewTimelineEntryProjection
    final: ReviewTimelineEntryProjection
    effect: ReviewTimelineEntryEditEffect
    result: ReviewTimelineEntryEditReceipt | None
    undo_available: bool
    undo_block_reason: ReviewTimelineEntryEditUndoBlockReason | None

    @model_validator(mode="after")
    def validate_state(self) -> "ReviewTimelineEntryEditOperationDTO":
        if self.state == "completed":
            if self.result is None or self.result.undo is not None:
                raise ValueError("completed review edit must expose apply receipt")
            if self.undo_available == (self.undo_block_reason is not None):
                raise ValueError("completed review edit undoability is inconsistent")
        elif self.state == "undone":
            if self.result is None or self.result.undo is None:
                raise ValueError("undone review edit must expose both receipts")
            if self.undo_available or self.undo_block_reason != "already_undone":
                raise ValueError("undone review edit cannot remain undoable")
        else:
            if self.result is not None or self.undo_available:
                raise ValueError("stale review edit cannot expose a trusted receipt")
            if self.undo_block_reason != "operation_invalid":
                raise ValueError("stale review edit must report invalid operation")
        return self
