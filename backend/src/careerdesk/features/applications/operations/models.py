"""Bounded durable contracts and DTOs for trusted application deletion."""

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

from ..intake_models import (
    ApplicationNextAction,
    ApplicationStage,
    ChannelText,
    CompanyText,
    DepartmentText,
    HighlightText,
    PositionText,
    SkillText,
    StepText,
)

APPLICATION_DELETE_CONTRACT_VERSION = 1
MAX_DELETE_TIMELINE_ENTRIES = 100
MAX_DELETE_QUESTIONS = 100
MAX_DELETE_RESUMES = 100
MAX_DELETE_JD_PREVIEW_CHARS = 4_000
MAX_DELETE_QUESTION_PREVIEW_CHARS = 500
MAX_DELETE_SKILLS = 100
MAX_DELETE_HIGHLIGHTS = 100
MAX_DELETE_PUBLIC_TEXT_CHARS = 250_000
MAX_DELETE_TIMELINE_SUMMARY_CHARS = 2_000
MAX_OPERATION_ERROR_CHARS = 500

BoundedTimestamp = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
JDPreview = Annotated[str, StringConstraints(max_length=MAX_DELETE_JD_PREVIEW_CHARS)]
TimelineSummary = Annotated[
    str,
    StringConstraints(max_length=MAX_DELETE_TIMELINE_SUMMARY_CHARS),
]
QuestionPreview = Annotated[str, StringConstraints(max_length=MAX_DELETE_QUESTION_PREVIEW_CHARS)]
ResumeName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ISODateText = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
OperationErrorText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_OPERATION_ERROR_CHARS),
]


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


class ApplicationDeleteResumeRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    name: ResumeName
    archived: bool


class ApplicationDeleteTarget(BaseModel):
    """Fully identified application state frozen before approval."""

    model_config = ConfigDict(extra="forbid", strict=True)

    application_id: int = Field(gt=0)
    company: CompanyText
    position: PositionText
    department: DepartmentText | None = None
    channel: ChannelText | None = None
    stage: ApplicationStage
    current_step: StepText | None = None
    priority: Literal["high", "medium", "low"] | None = None
    selected_resume: ApplicationDeleteResumeRef | None = None
    applied_date: ISODateText | None = None
    next_action: ApplicationNextAction | None = None
    paused_from_stage: ApplicationStage | None = None
    pause_reason: TimelineSummary | None = None
    application_note: TimelineSummary | None = None
    jd_preview: JDPreview
    jd_truncated: bool
    skills: list[SkillText] = Field(default_factory=list, max_length=MAX_DELETE_SKILLS)
    highlights: list[HighlightText] = Field(
        default_factory=list,
        max_length=MAX_DELETE_HIGHLIGHTS,
    )
    prep_status: Literal["none", "pending", "running", "ready", "failed"]
    prep_artifact_present: bool
    application_created_time: BoundedTimestamp
    application_updated_time: BoundedTimestamp

    @field_validator("applied_date")
    @classmethod
    def validate_real_dates(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)

    @model_validator(mode="after")
    def validate_preview_flag(self) -> "ApplicationDeleteTarget":
        if self.jd_truncated and len(self.jd_preview) != MAX_DELETE_JD_PREVIEW_CHARS:
            raise ValueError("truncated JD preview must fill its fixed budget")
        return self


class ApplicationDeleteTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    step: StepText | None = None
    occurred_date: ISODateText | None = None
    outcome: Literal["passed", "failed", "cancelled"] | None = None
    summary: TimelineSummary | None = None
    from_stage: ApplicationStage | None = None
    from_step: StepText | None = None
    to_stage: ApplicationStage | None = None
    to_step: StepText | None = None

    @field_validator("occurred_date")
    @classmethod
    def validate_real_date(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)

    @model_validator(mode="after")
    def require_meaningful_entry(self) -> "ApplicationDeleteTimelineEntry":
        if not any((
            self.step,
            self.outcome,
            self.summary,
            self.from_stage != self.to_stage,
            self.from_step != self.to_step,
        )):
            raise ValueError("timeline entry must contain a fact or state transition")
        return self


class ApplicationDeleteQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    text_preview: QuestionPreview
    text_truncated: bool
    source: Literal["real", "generated", "imported"]

    @model_validator(mode="after")
    def validate_preview_flag(self) -> "ApplicationDeleteQuestion":
        if (self.text_truncated
                and len(self.text_preview) != MAX_DELETE_QUESTION_PREVIEW_CHARS):
            raise ValueError("truncated question preview must fill its fixed budget")
        return self


class ApplicationDeleteResume(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    name: ResumeName
    binding: Literal["family", "application"]
    archived: bool


class ApplicationDeleteEffect(BaseModel):
    """Deletion and unlink effects the page must display item by item."""

    model_config = ConfigDict(extra="forbid", strict=True)

    timeline_entries: list[ApplicationDeleteTimelineEntry] = Field(
        default_factory=list,
        max_length=MAX_DELETE_TIMELINE_ENTRIES,
    )
    questions_detached: list[ApplicationDeleteQuestion] = Field(
        default_factory=list,
        max_length=MAX_DELETE_QUESTIONS,
    )
    question_occurrences_detached: int = Field(ge=0, le=10_000)
    resumes_detached: list[ApplicationDeleteResume] = Field(
        default_factory=list,
        max_length=MAX_DELETE_RESUMES,
    )
    selected_resume_retained: Literal[True]
    company_records_untouched: Literal[True]
    journal_records_untouched: Literal[True]
    external_logs_untouched: Literal[True]

    @model_validator(mode="after")
    def validate_unique_effect_rows(self) -> "ApplicationDeleteEffect":
        groups = (
            [entry.id for entry in self.timeline_entries],
            [question.id for question in self.questions_detached],
            [resume.id for resume in self.resumes_detached],
        )
        if any(len(values) != len(set(values)) for values in groups):
            raise ValueError("application delete effect contains duplicate row ids")
        public_text = sum(len(value or "") for value in (
            *(entry.summary for entry in self.timeline_entries),
            *(question.text_preview for question in self.questions_detached),
            *(resume.name for resume in self.resumes_detached),
        ))
        if public_text > MAX_DELETE_PUBLIC_TEXT_CHARS:
            raise ValueError("application delete public effect exceeds text budget")
        return self


class ApplicationDeleteProposal(BaseModel):
    """Immutable proposal persisted in correction journal extraction JSON."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["application_delete"]
    contract_version: Literal[APPLICATION_DELETE_CONTRACT_VERSION]
    dependency_fingerprint: Fingerprint
    target: ApplicationDeleteTarget
    effect: ApplicationDeleteEffect


class ApplicationDeleteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["ok"]
    application_id: int = Field(gt=0)
    timeline_entries_removed: int = Field(ge=0, le=MAX_DELETE_TIMELINE_ENTRIES)
    questions_detached: int = Field(ge=0, le=MAX_DELETE_QUESTIONS)
    question_occurrences_detached: int = Field(ge=0, le=10_000)
    resumes_detached: int = Field(ge=0, le=MAX_DELETE_RESUMES)


class ApplicationDeleteOperationError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,100}$")]
    message: OperationErrorText


class ApplicationDeleteOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: Annotated[
        str,
        StringConstraints(pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )),
    ]
    operation_type: Literal["application_delete"]
    state: Literal["pending", "completed", "rejected", "stale"]
    created_time: BoundedTimestamp
    target: ApplicationDeleteTarget
    effect: ApplicationDeleteEffect
    result: ApplicationDeleteResult | None

    @model_validator(mode="after")
    def validate_result_state(self) -> "ApplicationDeleteOperationDTO":
        if (self.state == "completed") != (self.result is not None):
            raise ValueError("only completed operations may expose a result")
        return self
