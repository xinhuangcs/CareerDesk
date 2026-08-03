"""Application preparation HTTP response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ...features.applications.contracts import ApplicationDetailResponse
from ...platform.locale import OutputLocale
from .adaptation_contracts import (
    MAX_MAJOR_GAPS,
    MAX_REFS_PER_ITEM,
    MAX_REQUIREMENT_ASSESSMENTS_FULL,
    MAX_REWRITES,
    MAX_SECTION_REVIEWS,
    MajorGap,
    RequirementAssessment,
    ResumeAdaptationReport,
    RewriteSuggestion,
    SectionReview,
)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchSectionArtifact(BaseModel):
    """Company report section with text and legacy or namespaced source IDs."""

    model_config = ConfigDict(extra="allow")

    text: str
    sources: list[int | str] = Field(default_factory=list)


class ResearchArtifact(BaseModel):
    """Source-cited company report compatible with v1 plain-text caches."""

    model_config = ConfigDict(extra="allow")

    business: ResearchSectionArtifact | str
    culture: ResearchSectionArtifact | str
    recent_news: ResearchSectionArtifact | str
    interview_style: ResearchSectionArtifact | str


class BriefingData(_Contract):
    application: ApplicationDetailResponse
    research: ResearchArtifact | None
    research_stale: bool
    position_report: dict | None = None
    anchor: dict | None = None
    sources: list[dict] = Field(default_factory=list)


class BriefingResponse(_Contract):
    status: Literal["ok"]
    markdown: str
    data: BriefingData


class PrepTriggerResponse(_Contract):
    status: Literal["started", "reused", "completed", "error"]
    message: str | None = None
    application_id: int | None = None
    prep_status: str | None = None
    reused: bool | None = None
    refresh_requested: bool | None = None
    refresh_applied: bool | None = None
    takeover_applied: bool | None = None
    retry_after_seconds: int | None = None


ResumeAdaptationState = Literal[
    "ready",
    "generation_running",
    "ok",
    "no_resume",
    "resume_selection_required",
    "resume_reupload_required",
    "missing_jd",
    "research_required",
    "research_running",
    "research_failed",
    "research_disabled",
    "research_unavailable",
    "model_required",
    "insufficient_model_capacity",
    "invalid_model_output",
    "stale",
    "provider_error",
]


class ResumeAdaptationGenerateRequest(_Contract):
    refresh: StrictBool = False
    expected_resume_id: int | None = Field(default=None, gt=0)
    accept_no_research: StrictBool = False
    accept_summarized: StrictBool = False
    output_locale: OutputLocale = "zh-CN"


class ResumeExtractionReceipt(_Contract):
    """Public extraction diagnostics; never expose resume text or file metadata."""

    status: Literal["usable", "reupload_required"]
    char_count: int = Field(ge=0)
    non_whitespace_count: int = Field(ge=0)
    alnum_count: int = Field(ge=0)
    replacement_char_count: int = Field(ge=0)
    replacement_ratio: float = Field(ge=0, le=1)
    control_char_count: int = Field(ge=0)
    control_ratio: float = Field(ge=0, le=1)
    reason_codes: list[str]
    warning_codes: list[str]


class ResumeAdaptationResume(_Contract):
    id: int = Field(gt=0)
    name: str
    updated_time: str | None
    extraction_receipt: ResumeExtractionReceipt | None


class ResumeAdaptationResearch(_Contract):
    artifact_state: Literal["missing", "ready", "stale", "legacy"]
    attempt_state: Literal[
        "idle",
        "pending",
        "running",
        "succeeded",
        "failed",
        "disabled",
        "unavailable",
    ]
    coverage_quality: Literal["complete", "partial", "insufficient"] | None
    fresh_until: str | None
    error_code: str | None
    action: Literal["start", "restart", "refresh", "retry"] | None


class ResumeAdaptationModelDisclosure(_Contract):
    provider: str
    model: str
    label: str


class ResumeAdaptationEnvelope(_Contract):
    artifact_version: int = Field(ge=0)
    resume_id: int = Field(gt=0)
    resume_name: str
    resume_selection: Literal["bound", "confirmed"]
    research_mode: Literal["snapshot", "no_research"]
    research_snapshot_id: str | None
    resume_input_form: Literal["full_text", "summarized"]
    generated_time: str
    content_locale: OutputLocale


class ResumeAdaptationEvidence(_Contract):
    """Host-materialized source text with exact offsets, never model-authored."""

    segment_id: str = Field(
        pattern=r"^[JR][1-9][0-9]*-[0-9]{4,}-[0-9a-f]{8}$",
        max_length=64,
    )
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(ge=0, strict=True)
    text: str

    @model_validator(mode="after")
    def validate_offsets(self) -> "ResumeAdaptationEvidence":
        if self.char_end < self.char_start:
            raise ValueError("evidence char_end 不能早于 char_start")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("evidence offsets 必须与宿主原文长度一致")
        return self


class ResumeAdaptationMaterializedRequirement(RequirementAssessment):
    jd_evidence: list[ResumeAdaptationEvidence] = Field(max_length=MAX_REFS_PER_ITEM)
    resume_evidence: list[ResumeAdaptationEvidence] = Field(max_length=MAX_REFS_PER_ITEM)


class ResumeAdaptationMaterializedRewrite(RewriteSuggestion):
    original_text: str


class ResumeAdaptationSegmentRange(_Contract):
    start: ResumeAdaptationEvidence
    end: ResumeAdaptationEvidence


class ResumeAdaptationMaterializedSection(SectionReview):
    section_id: str = Field(pattern=r"^S1-[0-9]{4,}-[0-9a-f]{8}$", max_length=64)
    resume_segment_range: ResumeAdaptationSegmentRange
    rewrites: list[ResumeAdaptationMaterializedRewrite] = Field(max_length=MAX_REWRITES)


class ResumeAdaptationMaterializedGap(MajorGap):
    jd_evidence: list[ResumeAdaptationEvidence] = Field(max_length=MAX_REFS_PER_ITEM)
    resume_evidence: list[ResumeAdaptationEvidence] = Field(max_length=MAX_REFS_PER_ITEM)


class ResumeAdaptationMaterializedReport(ResumeAdaptationReport):
    """Strict public superset of the provider report after trusted refill."""

    requirement_assessments: list[ResumeAdaptationMaterializedRequirement] = Field(
        min_length=1,
        max_length=MAX_REQUIREMENT_ASSESSMENTS_FULL,
    )
    section_reviews: list[ResumeAdaptationMaterializedSection] = Field(
        max_length=MAX_SECTION_REVIEWS,
    )
    major_gaps: list[ResumeAdaptationMaterializedGap] = Field(max_length=MAX_MAJOR_GAPS)


class ResumeAdaptationResponse(_Contract):
    state: ResumeAdaptationState
    message: str | None
    cached: bool
    bound_resume: ResumeAdaptationResume | None
    resume_options: list[ResumeAdaptationResume]
    recommended_resume_id: int | None = Field(default=None, gt=0)
    research: ResumeAdaptationResearch | None
    report: ResumeAdaptationMaterializedReport | None
    envelope: ResumeAdaptationEnvelope | None
    host_limitations: list[str]
    analysis_flags: list[str]
    estimated_input_tokens: int | None = Field(default=None, gt=0)
    model_disclosure: ResumeAdaptationModelDisclosure | None
    summarization_available: bool
    no_research_fallback_available: bool
    model_input_preview_available: bool

    @model_validator(mode="after")
    def validate_report_state(self) -> "ResumeAdaptationResponse":
        if self.state == "ok":
            if self.report is None or self.envelope is None or self.bound_resume is None:
                raise ValueError("ok state 必须包含报告、envelope 和已绑定简历")
            if (
                self.envelope.resume_id != self.bound_resume.id
                or self.envelope.resume_name != self.bound_resume.name
            ):
                raise ValueError("报告 envelope 必须匹配当前绑定简历")
        elif self.report is not None or self.envelope is not None:
            raise ValueError("非 ok state 不能携带报告或 envelope")
        return self


class ResumeAdaptationInputPreviewResponse(_Contract):
    resume_id: int = Field(gt=0)
    resume_name: str
    input_form: Literal["full_text", "summarized"]
    text: str
    host_limitations: list[str]
