"""Bounded durable contracts and DTOs for trusted application merge."""

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
    NextNoteText,
    PositionText,
    SkillText,
    StepText,
)
from .models import (
    MAX_DELETE_HIGHLIGHTS,
    MAX_DELETE_JD_PREVIEW_CHARS,
    MAX_DELETE_QUESTIONS,
    MAX_DELETE_QUESTION_PREVIEW_CHARS,
    MAX_DELETE_RESUMES,
    MAX_DELETE_SKILLS,
    MAX_DELETE_TIMELINE_ENTRIES,
    MAX_OPERATION_ERROR_CHARS,
)

APPLICATION_MERGE_CONTRACT_VERSION = 2
MAX_MERGE_OCCURRENCES = 1_000
MAX_MERGE_FIELD_SUMMARY_CHARS = MAX_DELETE_JD_PREVIEW_CHARS + 200
MAX_MERGE_PUBLIC_TEXT_CHARS = 500_000

BoundedTimestamp = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
ResumeName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
JDPreview = Annotated[str, StringConstraints(max_length=MAX_DELETE_JD_PREVIEW_CHARS)]
QuestionPreview = Annotated[
    str,
    StringConstraints(max_length=MAX_DELETE_QUESTION_PREVIEW_CHARS),
]
TimelineSummary = Annotated[str, StringConstraints(max_length=2_000)]
PauseReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000),
]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ISODateText = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
FieldSummary = Annotated[
    str,
    StringConstraints(max_length=MAX_MERGE_FIELD_SUMMARY_CHARS),
]
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


class ApplicationMergeResumeRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    name: ResumeName
    binding: Literal["family", "application"]
    application_id: int | None = Field(default=None, gt=0)
    archived: bool

    @model_validator(mode="after")
    def validate_binding(self) -> "ApplicationMergeResumeRef":
        if (self.binding == "application") != (self.application_id is not None):
            raise ValueError("resume binding and application_id disagree")
        return self


class ApplicationMergeApplication(BaseModel):
    """Public source/destination snapshot frozen before confirmation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    application_id: int = Field(gt=0)
    company: CompanyText
    position: PositionText
    department: DepartmentText | None = None
    channel: ChannelText | None = None
    stage: ApplicationStage
    current_step: StepText | None = None
    priority: Literal["high", "medium", "low"] | None = None
    selected_resume: ApplicationMergeResumeRef | None = None
    applied_date: ISODateText | None = None
    next_action: ApplicationNextAction | None = None
    paused_from_stage: ApplicationStage | None = None
    pause_reason: PauseReason | None = None
    application_note: NextNoteText | None = None
    jd_preview: JDPreview
    jd_truncated: bool
    skills: list[SkillText] = Field(default_factory=list, max_length=MAX_DELETE_SKILLS)
    highlights: list[HighlightText] = Field(
        default_factory=list,
        max_length=MAX_DELETE_HIGHLIGHTS,
    )
    prep_status: Literal["none", "pending", "running", "ready", "failed"]
    prep_artifact_present: bool
    revision: int = Field(ge=0)
    application_created_time: BoundedTimestamp
    application_updated_time: BoundedTimestamp

    @field_validator("applied_date")
    @classmethod
    def validate_real_dates(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ApplicationMergeApplication":
        if self.jd_truncated and len(self.jd_preview) != MAX_DELETE_JD_PREVIEW_CHARS:
            raise ValueError("truncated JD preview must fill its fixed budget")
        if (self.selected_resume is not None
                and self.selected_resume.binding == "application"
                and self.selected_resume.application_id != self.application_id):
            raise ValueError("application-specific selected resume belongs elsewhere")
        return self


class ApplicationMergeFinalDestination(BaseModel):
    """Exact post-approval destination projection; updated_time is execution-only."""

    model_config = ConfigDict(extra="forbid", strict=True)

    application_id: int = Field(gt=0)
    company: CompanyText
    position: PositionText
    department: DepartmentText | None = None
    channel: ChannelText | None = None
    stage: ApplicationStage
    current_step: StepText | None = None
    priority: Literal["high", "medium", "low"] | None = None
    selected_resume: ApplicationMergeResumeRef | None = None
    applied_date: ISODateText | None = None
    next_action: ApplicationNextAction | None = None
    paused_from_stage: ApplicationStage | None = None
    pause_reason: PauseReason | None = None
    application_note: NextNoteText | None = None
    jd_source: Literal["source", "destination", "none"]
    jd_preview: JDPreview
    jd_truncated: bool
    skills: list[SkillText] = Field(default_factory=list, max_length=MAX_DELETE_SKILLS)
    highlights: list[HighlightText] = Field(
        default_factory=list,
        max_length=MAX_DELETE_HIGHLIGHTS,
    )
    prep_status: Literal["none"]
    prep_artifact_present: Literal[False]

    @field_validator("applied_date")
    @classmethod
    def validate_real_dates(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)

    @model_validator(mode="after")
    def validate_projection(self) -> "ApplicationMergeFinalDestination":
        if self.jd_truncated and len(self.jd_preview) != MAX_DELETE_JD_PREVIEW_CHARS:
            raise ValueError("truncated JD preview must fill its fixed budget")
        if (self.selected_resume is not None
                and self.selected_resume.binding == "application"
                and self.selected_resume.application_id != self.application_id):
            raise ValueError("final selected resume is not bound to destination")
        if self.jd_source == "none" and (
            self.jd_preview or self.skills or self.highlights or self.jd_truncated
        ):
            raise ValueError("empty JD source cannot expose JD content")
        return self


class ApplicationMergeFieldResolution(BaseModel):
    """Fixed survivorship decision displayed item by item."""

    model_config = ConfigDict(extra="forbid", strict=True)

    field: Literal[
        "company", "position", "department", "channel", "stage", "current_step",
        "priority", "selected_resume", "applied_date", "next_action", "pause",
        "application_note", "jd", "prep",
    ]
    strategy: Literal[
        "destination_identity", "destination_preferred", "source_fallback",
        "highest_priority", "cleared_for_safety",
    ]
    source_value: FieldSummary | None = None
    destination_value: FieldSummary | None = None
    final_value: FieldSummary | None = None
    source_value_carried_forward: bool


class ApplicationMergeTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    step: StepText | None = None
    occurred_date: ISODateText | None = None
    outcome: Literal["passed", "failed", "cancelled"] | None = None
    summary: TimelineSummary | None = None
    from_stage: ApplicationStage
    from_step: StepText | None = None
    to_stage: ApplicationStage
    to_step: StepText | None = None
    source: Literal["manual", "agent", "review", "drag", "system"]
    journal_id: int | None = Field(default=None, gt=0)
    created_time: BoundedTimestamp

    @field_validator("occurred_date")
    @classmethod
    def validate_real_date(cls, value: str | None) -> str | None:
        return _validate_real_iso_date(value)

    @model_validator(mode="after")
    def validate_meaningful_entry(self) -> "ApplicationMergeTimelineEntry":
        if not any((
            self.step,
            self.outcome,
            self.summary,
            self.from_stage != self.to_stage,
            self.from_step != self.to_step,
        )):
            raise ValueError("timeline entry must record progress or a state change")
        return self


class ApplicationMergeQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    text_preview: QuestionPreview
    text_truncated: bool
    source: Literal["real", "generated", "imported"]
    company_before: CompanyText | None = None
    company_after: CompanyText
    journal_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_preview_flag(self) -> "ApplicationMergeQuestion":
        if (self.text_truncated
                and len(self.text_preview) != MAX_DELETE_QUESTION_PREVIEW_CHARS):
            raise ValueError("truncated question preview must fill its fixed budget")
        return self


class ApplicationMergeOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    journal_id: int = Field(gt=0)
    question_id: int = Field(gt=0)
    company_before: CompanyText
    company_after: CompanyText


class ApplicationMergeResume(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(gt=0)
    name: ResumeName
    binding: Literal["application"]
    archived: bool


class ApplicationMergeCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

# Each snapshot side is bounded; completion also reports original destination plus moved counts.
    timeline_entries: int = Field(ge=0, le=MAX_DELETE_TIMELINE_ENTRIES * 2)
    questions: int = Field(ge=0, le=MAX_DELETE_QUESTIONS * 2)
    question_occurrences: int = Field(ge=0, le=MAX_MERGE_OCCURRENCES * 2)
    resumes: int = Field(ge=0, le=MAX_DELETE_RESUMES * 2)


class ApplicationMergeEffect(BaseModel):
    """Merge impact displayed item by item and reconciled by completion receipt."""

    model_config = ConfigDict(extra="forbid", strict=True)

    final_destination: ApplicationMergeFinalDestination
    field_resolutions: list[ApplicationMergeFieldResolution] = Field(
        min_length=14,
        max_length=14,
    )
    timeline_entries_rebound: list[ApplicationMergeTimelineEntry] = Field(
        default_factory=list,
        max_length=MAX_DELETE_TIMELINE_ENTRIES,
    )
    questions_rebound: list[ApplicationMergeQuestion] = Field(
        default_factory=list,
        max_length=MAX_DELETE_QUESTIONS,
    )
    question_occurrences_rebound: list[ApplicationMergeOccurrence] = Field(
        default_factory=list,
        max_length=MAX_MERGE_OCCURRENCES,
    )
    resumes_rebound: list[ApplicationMergeResume] = Field(
        default_factory=list,
        max_length=MAX_DELETE_RESUMES,
    )
    destination_existing: ApplicationMergeCounts
    source_application_removed: Literal[True]
    destination_prep_reset: Literal[True]
    source_prep_removed_with_application: Literal[True]
    company_records_untouched: Literal[True]
    journal_records_untouched: Literal[True]
    external_logs_untouched: Literal[True]

    @model_validator(mode="after")
    def validate_effect(self) -> "ApplicationMergeEffect":
        fields = [resolution.field for resolution in self.field_resolutions]
        expected_fields = {
            "company", "position", "department", "channel", "stage", "current_step",
            "priority", "selected_resume", "applied_date", "next_action", "pause",
            "application_note", "jd", "prep",
        }
        if len(set(fields)) != len(fields) or set(fields) != expected_fields:
            raise ValueError("merge field resolutions must cover each field exactly once")
        groups = (
            [entry.id for entry in self.timeline_entries_rebound],
            [question.id for question in self.questions_rebound],
            [(item.journal_id, item.question_id) for item in self.question_occurrences_rebound],
            [resume.id for resume in self.resumes_rebound],
        )
        if any(len(values) != len(set(values)) for values in groups):
            raise ValueError("application merge effect contains duplicate dependency ids")
        public_text = sum(len(value or "") for value in (
            *(entry.summary for entry in self.timeline_entries_rebound),
            *(question.text_preview for question in self.questions_rebound),
            *(resume.name for resume in self.resumes_rebound),
            *(resolution.source_value for resolution in self.field_resolutions),
            *(resolution.destination_value for resolution in self.field_resolutions),
            *(resolution.final_value for resolution in self.field_resolutions),
        ))
        if public_text > MAX_MERGE_PUBLIC_TEXT_CHARS:
            raise ValueError("application merge public effect exceeds text budget")
        return self


class ApplicationMergeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_type: Literal["application_merge"]
    contract_version: Literal[APPLICATION_MERGE_CONTRACT_VERSION]
    dependency_fingerprint: Fingerprint
    source: ApplicationMergeApplication
    destination: ApplicationMergeApplication
    effect: ApplicationMergeEffect

    @model_validator(mode="after")
    def validate_direction(self) -> "ApplicationMergeProposal":
        if self.source.application_id == self.destination.application_id:
            raise ValueError("merge source and destination must differ")
        if self.effect.final_destination.application_id != self.destination.application_id:
            raise ValueError("final destination identity changed")
        return self


class ApplicationMergeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["ok"]
    source_application_id: int = Field(gt=0)
    destination_application_id: int = Field(gt=0)
    source_deleted: Literal[True]
    moved: ApplicationMergeCounts
    destination_totals: ApplicationMergeCounts
    destination_prep_reset: Literal[True]
    final_destination: ApplicationMergeFinalDestination


class ApplicationMergeOperationError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,100}$")]
    message: OperationErrorText


class ApplicationMergeOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: Annotated[
        str,
        StringConstraints(pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )),
    ]
    operation_type: Literal["application_merge"]
    state: Literal["pending", "completed", "rejected", "stale"]
    created_time: BoundedTimestamp
    source: ApplicationMergeApplication
    destination: ApplicationMergeApplication
    effect: ApplicationMergeEffect
    result: ApplicationMergeResult | None

    @model_validator(mode="after")
    def validate_result_state(self) -> "ApplicationMergeOperationDTO":
        if (self.state == "completed") != (self.result is not None):
            raise ValueError("only completed operations may expose a result")
        return self
