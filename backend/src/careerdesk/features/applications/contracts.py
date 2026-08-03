"""Applications HTTP contracts for state, next action, and factual history."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .intake_models import ApplicationNextAction, ApplicationStage, StepText
from .operations.merge_models import ApplicationMergeOperationDTO
from .operations.models import ApplicationDeleteOperationDTO

ApplicationPriority = Literal["high", "medium", "low"]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntakeFlagsDTO(_Contract):
    invalidate_prep: bool
    add_applied_entry: bool
    clear_next_action: bool


class IntakePositionDTO(_Contract):
    mode: Literal["create", "update"]
    company: str
    position: str
    department: str | None
    channel: str | None
    jd_text: str | None
    skills: list[str]
    highlights: list[str]
    stage: ApplicationStage
    current_step: str | None
    applied_date: str | None
    pause_reason: str | None
    next_action: ApplicationNextAction | None
    application_note: str | None
    priority: ApplicationPriority | None
    flags: IntakeFlagsDTO
    already_exists: bool


class IntakeApplicationRef(_Contract):
    id: int = Field(gt=0)
    company: str
    position: str


class IntakeApplyResult(_Contract):
    status: Literal["ok"]
    created: list[IntakeApplicationRef]
    updated: list[IntakeApplicationRef]


class IntakeOperationDTO(_Contract):
    operation_id: UUID
    state: Literal["pending", "completed", "rejected", "stale"]
    positions: list[IntakePositionDTO]
    source_rows: int = Field(ge=0)
    skipped_rows: int = Field(ge=0)
    created_time: str
    exclude_indexes: list[int] | None = None
    result: IntakeApplyResult | None = None


class IntakeOperationsResponse(_Contract):
    operations: list[IntakeOperationDTO]


class ApplicationDeleteOperationsResponse(_Contract):
    operations: list[ApplicationDeleteOperationDTO]


class ApplicationMergeOperationsResponse(_Contract):
    operations: list[ApplicationMergeOperationDTO]


class JDParsedDTO(_Contract):
    skills: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)


class ApplicationNextActionDTO(_Contract):
    stage: ApplicationStage
    step: str
    date: str | None
    time: str | None
    note: str | None


TimelineOutcome = Literal["passed", "failed", "cancelled"]
TimelineSource = Literal["manual", "agent", "review", "drag", "system"]


class TimelineEntryDTO(_Contract):
    id: int = Field(gt=0)
    step: str | None
    occurred_date: str | None
    outcome: TimelineOutcome | None
    summary: str | None
    from_stage: ApplicationStage
    from_step: str | None
    to_stage: ApplicationStage
    to_step: str | None
    source: TimelineSource
    created_time: str
    display_time: str
    snapshot_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class BoardItemDTO(_Contract):
    id: int = Field(gt=0)
    company: str
    position: str
    department: str | None
    channel: str | None
    stage: ApplicationStage
    current_step: str | None
    next_action: ApplicationNextActionDTO | None
    paused_from_stage: ApplicationStage | None
    pause_reason: str | None = Field(max_length=1_000)
    priority: ApplicationPriority | None
    applied_date: str | None
    prep_status: str
    revision: int = Field(ge=0)
    created_time: str
    last_activity_time: str | None = None

    @model_validator(mode="after")
    def validate_pause_projection(self) -> "BoardItemDTO":
        if self.stage != "pooled" and (
            self.paused_from_stage is not None or self.pause_reason is not None
        ):
            raise ValueError("非泡池子阶段不能保留暂停元数据")
        if self.paused_from_stage is not None and self.paused_from_stage not in {
            "backlog", "applied", "written_test", "interviewing", "offer",
        }:
            raise ValueError("泡池恢复阶段必须是进行中的阶段")
        return self


class BoardResponse(_Contract):
    columns: dict[str, list[BoardItemDTO]]
    total: int = Field(ge=0)


class TimelineFunnelDTO(_Contract):
    submitted: int = Field(ge=0)
    written_test: int = Field(ge=0)
    interviewing: int = Field(ge=0)
    offer: int = Field(ge=0)
    rejected: int = Field(ge=0)


class TimelineStatisticsResponse(_Contract):
    total_positions: int = Field(ge=0)
    submitted: int = Field(ge=0)
    active_processes: int = Field(ge=0)
    offers: int = Field(ge=0)
    rejected: int = Field(ge=0)
    withdrawn: int = Field(ge=0)
    pooled: int = Field(ge=0)
    interview_conversion_percent: float = Field(ge=0, le=100)
    offer_conversion_percent: float = Field(ge=0, le=100)
    funnel: TimelineFunnelDTO


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class ApplicationCreateRequest(_Contract):
    company: str = Field(min_length=1, max_length=200)
    position: str = Field(min_length=1, max_length=300)
    department: str | None = Field(default=None, max_length=200)
    channel: str | None = Field(default=None, max_length=100)
    stage: ApplicationStage = "backlog"
    priority: ApplicationPriority | None = None
    current_step: StepText | None = None
    applied_date: str | None = None
    pause_reason: str | None = Field(default=None, max_length=1_000)
    next_action: ApplicationNextAction | None = None
    jd_text: str | None = Field(default=None, max_length=50_000)

    @field_validator("company", "position")
    @classmethod
    def normalize_required_profile_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("公司名和岗位名不能为空")
        return normalized

    @field_validator("department", "channel", "pause_reason", "jd_text")
    @classmethod
    def normalize_optional_profile_text(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("applied_date")
    @classmethod
    def validate_applied_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("投递日期必须是真实的 YYYY-MM-DD") from error
        if parsed.isoformat() != value:
            raise ValueError("投递日期必须是真实的 YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def reject_closed_next_action(self) -> "ApplicationCreateRequest":
        if self.stage in {"rejected", "withdrawn"} and self.next_action is not None:
            raise ValueError("已挂或不再跟进的岗位不能保留下一步")
        if self.stage != "pooled" and self.pause_reason is not None:
            raise ValueError("只有泡池子阶段可以填写暂停原因")
        return self


class ApplicationProfileUpdateRequest(_Contract):
    """Full profile snapshot; nullable fields are required to prevent silent resets."""

    expected_revision: int = Field(ge=0)
    company: str = Field(min_length=1, max_length=200)
    position: str = Field(min_length=1, max_length=300)
    department: str | None = Field(max_length=200)
    channel: str | None = Field(max_length=100)
    stage: ApplicationStage
    # Omission preserves the stored priority; explicit null clears it.
    priority: ApplicationPriority | None = None
    current_step: StepText | None
    # Omission preserves the stored value; explicit null corrects it to unknown.
    applied_date: str | None = None
    pause_reason: str | None = Field(default=None, max_length=1_000)
    next_action: ApplicationNextAction | None
    jd_text: str | None = Field(max_length=50_000)

    _normalize_required_profile_text = field_validator("company", "position")(
        ApplicationCreateRequest.normalize_required_profile_text.__func__
    )
    _normalize_optional_profile_text = field_validator(
        "department", "channel", "pause_reason", "jd_text",
    )(ApplicationCreateRequest.normalize_optional_profile_text.__func__)
    _validate_applied_date = field_validator("applied_date")(
        ApplicationCreateRequest.validate_applied_date.__func__
    )

    @model_validator(mode="after")
    def reject_closed_next_action(self) -> "ApplicationProfileUpdateRequest":
        if self.stage in {"rejected", "withdrawn"} and self.next_action is not None:
            raise ValueError("已挂或不再跟进的岗位不能保留下一步")
        if self.stage != "pooled" and self.pause_reason is not None:
            raise ValueError("只有泡池子阶段可以填写暂停原因")
        return self


class ApplicationStageMoveRequest(_Contract):
    expected_revision: int = Field(ge=0)
    stage: ApplicationStage
    origin: Literal["board_drag", "detail_menu"]


class ApplicationNextActionUpdateRequest(_Contract):
    expected_revision: int = Field(ge=0)
    next_action: ApplicationNextAction | None


class ApplicationProgressWriteRequest(_Contract):
    expected_revision: int = Field(ge=0)
    step: StepText | None = None
    occurred_date: str | None = None
    outcome: TimelineOutcome | None = None
    summary: str | None = Field(default=None, max_length=2_000)
    update_current_state: bool = True
    target_stage: ApplicationStage | None = None
    target_step: StepText | None = None
    next_action: ApplicationNextAction | None = None

    @field_validator("occurred_date")
    @classmethod
    def validate_occurred_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("历程日期必须是真实的 YYYY-MM-DD") from error
        if parsed.isoformat() != value:
            raise ValueError("历程日期必须是真实的 YYYY-MM-DD")
        return value

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_progress(self) -> "ApplicationProgressWriteRequest":
        if not self.update_current_state and (
            self.target_stage is not None or self.target_step is not None
        ):
            raise ValueError("不更新当前状态时不能提交目标阶段或环节")
        if not any((self.step, self.summary, self.outcome, self.target_stage, self.target_step)):
            raise ValueError("进展至少要有环节、结果、说明或状态变化")
        return self


class CompleteNextActionRequest(_Contract):
    expected_revision: int = Field(ge=0)
    occurred_date: str | None = None
    outcome: TimelineOutcome | None = None
    summary: str | None = Field(default=None, max_length=2_000)
    next_action: ApplicationNextAction | None = None

    _validate_occurred_date = field_validator("occurred_date")(
        ApplicationProgressWriteRequest.validate_occurred_date.__func__
    )
    _normalize_summary = field_validator("summary")(
        ApplicationProgressWriteRequest.normalize_summary.__func__
    )


class TimelineEntryUpdateRequest(_Contract):
    expected_revision: int = Field(ge=0)
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    step: StepText | None = None
    occurred_date: str | None = None
    outcome: TimelineOutcome | None = None
    summary: str | None = Field(default=None, max_length=2_000)

    _validate_occurred_date = field_validator("occurred_date")(
        ApplicationProgressWriteRequest.validate_occurred_date.__func__
    )
    _normalize_summary = field_validator("summary")(
        ApplicationProgressWriteRequest.normalize_summary.__func__
    )


class ResearchAttemptMetadataDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_state: str | None = None
    generation: str | None = None
    updated_time: str | None = None
    error_code: str | None = None


class ResearchSnapshotMetadataDTO(BaseModel):
    """Lightweight Timeline projection; reports and sources use dedicated seams."""

    model_config = ConfigDict(extra="ignore")

    snapshot_version: int | None = None
    snapshot_id: str | None = None
    semantic_claim_hash: str | None = None
    coverage_quality: str | None = None
    company_report_hash: str | None = None
    company_report_generated_time: str | None = None
    position_report_hash: str | None = None
    position_report_generated_time: str | None = None
    missing_sections: list[str] = Field(default_factory=list)
    coverage_limitations: list[str] = Field(default_factory=list)
    fresh_until: str | None = None


class ResumeAdaptationMetadataDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    artifact_version: int | None = None
    input_hash: str | None = None
    resume_id: int | None = None
    resume_name: str | None = None
    research_mode: str | None = None
    research_snapshot_id: str | None = None
    resume_input_form: str | None = None
    generated_time: str | None = None
    analysis_flags: list[str] = Field(default_factory=list)


class ApplicationPrepArtifact(BaseModel):
    """HTTP-only metadata projection; unknown/internal large artifacts are intentionally dropped."""

    model_config = ConfigDict(extra="ignore")

    error: str | None = None
    research: str | None = None
    prepared_time: str | None = None
    research_attempt: ResearchAttemptMetadataDTO | None = None
    research_snapshot: ResearchSnapshotMetadataDTO | None = None
    resume_adaptation: ResumeAdaptationMetadataDTO | None = None

    @field_validator("research_attempt", "research_snapshot", "resume_adaptation", mode="before")
    @classmethod
    def ignore_malformed_metadata(cls, value):
        return value if isinstance(value, dict) else None


class ApplicationDetailResponse(BoardItemDTO):
    jd_text: str | None
    jd_parsed: JDParsedDTO
    resume_id: int | None
    updated_time: str
    prep: ApplicationPrepArtifact | None
    prep_retry_after_seconds: int | None
    timeline_entries: list[TimelineEntryDTO]
    application_note: str | None


class ApplicationResumeBindingRequest(_Contract):
    """Explicit application edit CAS for selecting the submitted resume."""

    # Nullable means callers can explicitly unbind; omission must not silently
    # turn a partial/malformed request into that destructive user action.
    resume_id: int | None = Field(gt=0)
    expected_edit_revision: int = Field(ge=0)


class BoundResumeDTO(_Contract):
    id: int = Field(gt=0)
    name: str
    updated_time: str | None
    extraction_receipt: dict | None


class ApplicationResumeBindingResponse(_Contract):
    resume_id: int | None = Field(default=None, gt=0)
    edit_revision: int = Field(ge=0)
    bound_resume: BoundResumeDTO | None


class ApplicationNoteRequest(_Contract):
    expected_revision: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=2_000)

    _normalize_note = field_validator("note")(_normalize_optional_text)


class UpcomingResponse(_Contract):
    days: int = Field(ge=1, le=60)
    items: list[BoardItemDTO]
