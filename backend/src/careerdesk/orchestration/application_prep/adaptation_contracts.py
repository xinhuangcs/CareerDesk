"""Bounded structured-output contracts for resume adaptation.

The model returns references into host-owned JD/resume segments.  It never
returns source text as a trusted value; :mod:`adaptation` validates and
materializes those references after the structured call.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


ADAPTATION_SCHEMA_VERSION = 1
ADAPTATION_PROMPT_VERSION = 3
ADAPTATION_RUBRIC_VERSION = 2
ADAPTATION_SEGMENT_VERSION = 1

ADAPTATION_TASK_OUTPUT_TOKENS = 16_384
ADAPTATION_FULL_MAX_TEXT_CHARS = 8_000
ADAPTATION_GAP_MAX_TEXT_CHARS = 4_000
ADAPTATION_OUTPUT_STRUCTURE_TOKENS = 4_000
ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS = 16_384

MAX_SUMMARY_SENTENCES = 3
MAX_SUMMARY_SENTENCE_CHARS = 240
MAX_REQUIREMENT_ASSESSMENTS_FULL = 12
MAX_REQUIREMENT_ASSESSMENTS_GAP = 5
MAX_REQUIREMENT_SUMMARY_CHARS = 240
MAX_REQUIREMENT_LIMITATION_CHARS = 300
MAX_REFS_PER_ITEM = 8
MAX_OVERALL_ADVICE = 5
MAX_ADVICE_ACTION_CHARS = 400
MAX_ADVICE_REASON_CHARS = 500
MAX_SECTION_REVIEWS = 40
MAX_SECTION_NAME_CHARS = 100
MAX_SECTION_CONCLUSION_CHARS = 400
MAX_SECTION_REASONING_CHARS = 800
MAX_PREPARATION_POINTS = 3
MAX_PREPARATION_POINT_CHARS = 400
MAX_IMPROVEMENTS = 3
MAX_IMPROVEMENT_CHARS = 500
MAX_REWRITES = 3
MAX_REWRITE_SUGGESTION_CHARS = 1_200
MAX_REWRITE_REASON_CHARS = 400
MAX_MAJOR_GAPS = 3
MAX_MAJOR_GAP_BASIS_CHARS = 500
MAX_NEXT_STEPS = 3
MAX_NEXT_STEP_CHARS = 500
MAX_ANALYSIS_CAVEATS = 5
MAX_ANALYSIS_CAVEAT_CHARS = 400
MAX_RESUME_SUMMARY_CHARS = 8_000

SUMMARY_CONFIRMATION_NOTICE = (
    "摘要降级会额外调用模型；超长文件可能分块多次。"
    "报告将不包含逐段点评或原文对照改写。"
)


def _bounded_text(max_length: int):
    return Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length),
    ]


SummarySentence = _bounded_text(MAX_SUMMARY_SENTENCE_CHARS)
RequirementSummary = _bounded_text(MAX_REQUIREMENT_SUMMARY_CHARS)
RequirementLimitation = _bounded_text(MAX_REQUIREMENT_LIMITATION_CHARS)
AdviceAction = _bounded_text(MAX_ADVICE_ACTION_CHARS)
AdviceReason = _bounded_text(MAX_ADVICE_REASON_CHARS)
SectionName = _bounded_text(MAX_SECTION_NAME_CHARS)
SectionConclusion = _bounded_text(MAX_SECTION_CONCLUSION_CHARS)
SectionReasoning = _bounded_text(MAX_SECTION_REASONING_CHARS)
PreparationPoint = _bounded_text(MAX_PREPARATION_POINT_CHARS)
Improvement = _bounded_text(MAX_IMPROVEMENT_CHARS)
RewriteSuggestionText = _bounded_text(MAX_REWRITE_SUGGESTION_CHARS)
RewriteReason = _bounded_text(MAX_REWRITE_REASON_CHARS)
MajorGapBasis = _bounded_text(MAX_MAJOR_GAP_BASIS_CHARS)
NextStep = _bounded_text(MAX_NEXT_STEP_CHARS)
AnalysisCaveat = _bounded_text(MAX_ANALYSIS_CAVEAT_CHARS)
ResumeSummaryText = _bounded_text(MAX_RESUME_SUMMARY_CHARS)

JDSegmentRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^J[1-9][0-9]*-[0-9]{4,}-[0-9a-f]{8}$",
        max_length=64,
    ),
]
ResumeSegmentRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^R[1-9][0-9]*-[0-9]{4,}-[0-9a-f]{8}$",
        max_length=64,
    ),
]


_COMMON_ABBREVIATIONS = frozenset(
    {
        "e.g.",
        "i.e.",
        "etc.",
        "mr.",
        "mrs.",
        "ms.",
        "dr.",
        "prof.",
        "vs.",
        "inc.",
        "ltd.",
        "co.",
        "no.",
        "st.",
        "fig.",
    }
)
_INITIALISM_RE = re.compile(r"(?:[A-Za-z]\.){2,}$")


def _period_token(value: str, index: int) -> str:
    start = index
    while start > 0 and (value[start - 1].isalpha() or value[start - 1] == "."):
        start -= 1
    return value[start : index + 1]


def validate_summary_sentence(value: str) -> str:
    """Require one list item to contain exactly one, terminal sentence boundary.

    Decimal/version dots, common abbreviations, initialisms and dots without a
    following whitespace boundary remain legal.  The deliberately conservative
    rule rejects ambiguous multi-sentence prose instead of trying to repair it.
    """

    if "\n" in value or "\r" in value:
        raise ValueError("总结句必须保持单行")
    if value[-1] not in "。！？!?.":
        raise ValueError("总结句必须以句号、问号或感叹号结束")

    for index, character in enumerate(value):
        if character in "。！？!?":
            if index != len(value) - 1:
                raise ValueError("每个总结元素只能包含一个末尾句界")
            continue
        if character != "." or index == len(value) - 1:
            continue
        previous = value[index - 1] if index else ""
        following = value[index + 1]
        if previous.isdigit() and following.isdigit():
            continue
        if not following.isspace():
            continue
        token = _period_token(value, index)
        if token.casefold() in _COMMON_ABBREVIATIONS or _INITIALISM_RE.fullmatch(token):
            continue
        if value[index + 1 :].strip():
            raise ValueError("每个总结元素只能包含一个末尾句界")
    return value


def _reject_duplicate_refs(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("同一字段不能重复引用 segment")
    return values


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementAssessment(_Contract):
    requirement_summary: RequirementSummary
    requirement_kind: Literal["must", "preferred", "context"]
    evidence_state: Literal["strong", "partial", "absent", "uncertain"]
    jd_segment_refs: list[JDSegmentRef] = Field(min_length=1, max_length=MAX_REFS_PER_ITEM)
    resume_segment_refs: list[ResumeSegmentRef] = Field(max_length=MAX_REFS_PER_ITEM)
    limitation: RequirementLimitation

    _unique_jd_refs = field_validator("jd_segment_refs")(_reject_duplicate_refs)
    _unique_resume_refs = field_validator("resume_segment_refs")(_reject_duplicate_refs)


class AdviceItem(_Contract):
    action: AdviceAction
    reason: AdviceReason


class RewriteSuggestion(_Contract):
    resume_segment_ref: ResumeSegmentRef
    suggestion: RewriteSuggestionText
    reason: RewriteReason
    verification_needed: bool


class SectionReview(_Contract):
    section_name: SectionName
    resume_segment_start_ref: ResumeSegmentRef
    resume_segment_end_ref: ResumeSegmentRef
    assessment: Literal[
        "highly_aligned",
        "aligned",
        "needs_work",
        "keep",
        "administrative",
    ]
    conclusion: SectionConclusion
    reasoning: SectionReasoning
    preparation_points: list[PreparationPoint] = Field(max_length=MAX_PREPARATION_POINTS)
    improvements: list[Improvement] = Field(max_length=MAX_IMPROVEMENTS)
    rewrites: list[RewriteSuggestion] = Field(max_length=MAX_REWRITES)

    @field_validator("rewrites")
    @classmethod
    def unique_rewrite_refs(
        cls,
        values: list[RewriteSuggestion],
    ) -> list[RewriteSuggestion]:
        refs = [item.resume_segment_ref for item in values]
        if len(refs) != len(set(refs)):
            raise ValueError("同一区块不能重复改写同一个 segment")
        return values


class MajorGap(_Contract):
    requirement_summary: RequirementSummary
    evidence_state: Literal["partial", "absent", "uncertain"]
    jd_segment_refs: list[JDSegmentRef] = Field(min_length=1, max_length=MAX_REFS_PER_ITEM)
    resume_segment_refs: list[ResumeSegmentRef] = Field(max_length=MAX_REFS_PER_ITEM)
    basis: MajorGapBasis

    _unique_jd_refs = field_validator("jd_segment_refs")(_reject_duplicate_refs)
    _unique_resume_refs = field_validator("resume_segment_refs")(_reject_duplicate_refs)


def report_text_char_count(report: "ResumeAdaptationReport") -> int:
    """Count model-authored, user-facing text while excluding enum/ref plumbing."""

    total = sum(map(len, report.summary_sentences))
    total += sum(
        len(item.requirement_summary) + len(item.limitation)
        for item in report.requirement_assessments
    )
    total += sum(len(item.action) + len(item.reason) for item in report.overall_advice)
    for section in report.section_reviews:
        total += len(section.section_name) + len(section.conclusion) + len(section.reasoning)
        total += sum(map(len, section.preparation_points))
        total += sum(map(len, section.improvements))
        total += sum(len(item.suggestion) + len(item.reason) for item in section.rewrites)
    total += sum(
        len(item.requirement_summary) + len(item.basis)
        for item in report.major_gaps
    )
    total += sum(map(len, report.next_steps))
    total += sum(map(len, report.analysis_caveats))
    return total


class ResumeAdaptationReport(_Contract):
    """One provider-portable root with host-context validation after the call."""

    mode: Literal["full", "gap_brief"]
    fit_band: Literal["strong", "promising", "weak"]
    summary_sentences: list[SummarySentence] = Field(
        min_length=1,
        max_length=MAX_SUMMARY_SENTENCES,
    )
    requirement_assessments: list[RequirementAssessment] = Field(
        min_length=1,
        max_length=MAX_REQUIREMENT_ASSESSMENTS_FULL,
    )
    overall_advice: list[AdviceItem] = Field(max_length=MAX_OVERALL_ADVICE)
    # A summarized/full report deliberately has no section reviews.  Whether
    # zero is legal therefore depends on the frozen input form and is enforced
    # by the host validator rather than this context-free provider schema.
    section_reviews: list[SectionReview] = Field(max_length=MAX_SECTION_REVIEWS)
    major_gaps: list[MajorGap] = Field(max_length=MAX_MAJOR_GAPS)
    next_steps: list[NextStep] = Field(max_length=MAX_NEXT_STEPS)
    analysis_caveats: list[AnalysisCaveat] = Field(max_length=MAX_ANALYSIS_CAVEATS)

    @field_validator("summary_sentences")
    @classmethod
    def validate_sentence_boundaries(cls, values: list[str]) -> list[str]:
        return [validate_summary_sentence(value) for value in values]

    @model_validator(mode="after")
    def validate_mode_and_text_budget(self) -> "ResumeAdaptationReport":
        if self.mode == "full":
            if self.fit_band not in {"strong", "promising"}:
                raise ValueError("full 模式只允许 strong/promising")
            if not self.overall_advice:
                raise ValueError("full 模式必须提供 overall_advice")
            if self.major_gaps or self.next_steps:
                raise ValueError("full 模式不能提供 gap_brief 字段")
            text_limit = ADAPTATION_FULL_MAX_TEXT_CHARS
        else:
            if self.fit_band != "weak":
                raise ValueError("gap_brief 模式只允许 weak")
            if len(self.requirement_assessments) > MAX_REQUIREMENT_ASSESSMENTS_GAP:
                raise ValueError("gap_brief 的 requirement_assessments 最多 5 项")
            if self.overall_advice or self.section_reviews:
                raise ValueError("gap_brief 不能提供 overall_advice/section_reviews")
            if not self.major_gaps or not self.next_steps:
                raise ValueError("gap_brief 必须提供 major_gaps 和 next_steps")
            text_limit = ADAPTATION_GAP_MAX_TEXT_CHARS

        if report_text_char_count(self) > text_limit:
            raise ValueError(f"模型文本合计不能超过 {text_limit:,} 个字符")
        return self


class ResumeSummaryResult(_Contract):
    """A summary whose model-returned target is rechecked against the host request."""

    target_chars: int = Field(ge=1, le=MAX_RESUME_SUMMARY_CHARS, strict=True)
    summary_text: ResumeSummaryText

    @model_validator(mode="after")
    def validate_target_length(self) -> "ResumeSummaryResult":
        if len(self.summary_text) > self.target_chars:
            raise ValueError("summary_text 超过 target_chars")
        return self

    def require_requested_target(self, requested_target_chars: int) -> "ResumeSummaryResult":
        if self.target_chars != requested_target_chars:
            raise ValueError("摘要返回的 target_chars 与宿主请求不一致")
        return self


__all__ = [
    "ADAPTATION_FULL_MAX_TEXT_CHARS",
    "ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS",
    "ADAPTATION_GAP_MAX_TEXT_CHARS",
    "ADAPTATION_PROMPT_VERSION",
    "ADAPTATION_RUBRIC_VERSION",
    "ADAPTATION_SCHEMA_VERSION",
    "ADAPTATION_SEGMENT_VERSION",
    "ADAPTATION_TASK_OUTPUT_TOKENS",
    "MAX_RESUME_SUMMARY_CHARS",
    "SUMMARY_CONFIRMATION_NOTICE",
    "AdviceItem",
    "MajorGap",
    "RequirementAssessment",
    "ResumeAdaptationReport",
    "ResumeSummaryResult",
    "RewriteSuggestion",
    "SectionReview",
    "report_text_char_count",
    "validate_summary_sentence",
]
