"""Strict, industry-neutral structured contracts for interview question sets."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

QuestionCategory = Literal[
    "hr_motivation", "resume_deep_dive", "behavioral_situational",
    "professional_domain", "business_company", "case_work_sample",
]
BasisKind = Literal["universal", "resume", "jd"]

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
QuestionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
GuideText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceRef(_StrictModel):
    basis_kind: BasisKind
    ref_id: str = Field(min_length=1, max_length=80)


class Rubric(_StrictModel):
    essential_criteria: list[ShortText] = Field(min_length=1, max_length=6)
    quality_signals: list[ShortText] = Field(max_length=6)
    critical_errors: list[ShortText] = Field(max_length=6)


class GeneratedQuestion(_StrictModel):
    text: QuestionText
    category: QuestionCategory
    channel: Literal["interview", "written"]
    response_format: Literal["oral_text", "short_written", "long_written", "case_outline"]
    difficulty: Literal["introductory", "intermediate", "advanced"]
    basis_kinds: list[BasisKind] = Field(min_length=1, max_length=5)
    evidence_refs: list[EvidenceRef] = Field(max_length=12)
    limitations: list[ShortText] = Field(max_length=6)
    primary_competency: ShortText
    secondary_tags: list[ShortText] = Field(max_length=4)
    evaluation_kind: Literal["evidence_consistency", "factual", "rubric", "case"]
    rubric: Rubric
    answer_guide: GuideText
    follow_up_allowed: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_shape(self) -> "GeneratedQuestion":
        if len(self.basis_kinds) != len(set(self.basis_kinds)):
            raise ValueError("basis_kinds cannot repeat")
        if self.channel == "written" and self.follow_up_allowed:
            raise ValueError("written questions cannot request follow-up")
        if self.channel == "interview" and self.response_format != "oral_text":
            raise ValueError("interview questions use oral_text")
        if self.channel == "written" and self.response_format == "oral_text":
            raise ValueError("written questions cannot use oral_text")
        if self.basis_kinds == ["universal"] and self.evidence_refs:
            raise ValueError("universal-only questions cannot cite material")
        if self.basis_kinds != ["universal"] and not self.evidence_refs:
            raise ValueError("material-grounded questions require refs")
        return self


class CoverageReport(_StrictModel):
    processed_sources: list[Literal["resume", "jd"]] = Field(min_length=1, max_length=2)
    covered_categories: list[QuestionCategory] = Field(max_length=6)
    omitted_categories: list[QuestionCategory] = Field(max_length=6)
    omission_reasons: list[ShortText] = Field(max_length=12)
    limitations: list[ShortText] = Field(max_length=12)


class GeneratedQuestionSet(_StrictModel):
    questions: list[GeneratedQuestion] = Field(max_length=30)
    coverage: CoverageReport


class SummaryPoint(_StrictModel):
    text: GuideText
    refs: list[EvidenceRef] = Field(min_length=1, max_length=8)


class MaterialSummary(_StrictModel):
    points: list[SummaryPoint] = Field(min_length=1, max_length=80)
    limitations: list[ShortText] = Field(max_length=12)


__all__ = [
    "BasisKind", "CoverageReport", "EvidenceRef", "GeneratedQuestion",
    "GeneratedQuestionSet", "MaterialSummary", "QuestionCategory", "Rubric",
]
