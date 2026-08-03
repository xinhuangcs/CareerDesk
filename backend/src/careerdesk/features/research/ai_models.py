"""Bounded structured output models for company and role research.

Conclusions may cite only host-numbered sources; the model is never a source authority.
"""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# Standard insufficient-material placeholder used by missing-section detection.
NOT_FOUND = "未找到公开信息"
NOT_FOUND_EN = "No public information found"
MAX_RESEARCH_SECTION_CHARS = 6_000
MAX_RESEARCH_QUESTIONS = 8
MAX_RESEARCH_QUESTION_CHARS = 1_000
MAX_RESEARCH_TOTAL_TEXT_CHARS = 32_000
MAX_SECTION_SOURCES = 12
MAX_RESEARCH_CONFLICTS = 5
MAX_RESEARCH_CONFLICT_SUMMARY_CHARS = 400
MAX_ANCHOR_FIELD_CHARS = 300
MAX_ANCHOR_NOTE_CHARS = 600
MAX_PLAN_QUERIES = 24
MAX_PLAN_QUERY_CHARS = 120
MAX_KEY_TAKEAWAYS = 5
MAX_KEY_TAKEAWAY_CHARS = 400
MAX_TECH_QUESTIONS = 12
MAX_TECH_QUESTION_CHARS = 600

ResearchSectionText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_RESEARCH_SECTION_CHARS,
    ),
]
ResearchQuestionText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_RESEARCH_QUESTION_CHARS,
    ),
]
ResearchConflictSummary = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_RESEARCH_CONFLICT_SUMMARY_CHARS,
    ),
]


AnchorFieldText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=MAX_ANCHOR_FIELD_CHARS),
]
KeyTakeawayText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1,
                      max_length=MAX_KEY_TAKEAWAY_CHARS),
]
ResearchQuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1,
                      max_length=MAX_TECH_QUESTION_CHARS),
]
PlanQueryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1,
                      max_length=MAX_PLAN_QUERY_CHARS),
]


def _stable_unique_sources(values: list[int]) -> list[int]:
    return list(dict.fromkeys(values))


class CitedSection(BaseModel):
    """Report section with one-based host source references."""

    model_config = ConfigDict(extra="forbid")

    text: ResearchSectionText
    sources: list[int] = Field(default_factory=list, max_length=MAX_SECTION_SOURCES)

    @field_validator("sources")
    @classmethod
    def stable_unique(cls, values: list[int]) -> list[int]:
        for value in values:
            if value < 1:
                raise ValueError("source 索引必须从 1 开始")
        return _stable_unique_sources(values)


class ResearchConflict(BaseModel):
    """A material-supported contradiction; both sides must remain auditable."""

    model_config = ConfigDict(extra="forbid")

    summary: ResearchConflictSummary
    sources: list[int] = Field(
        min_length=2,
        max_length=MAX_SECTION_SOURCES,
    )

    @field_validator("sources")
    @classmethod
    def stable_unique(cls, values: list[int]) -> list[int]:
        unique = _stable_unique_sources(values)
        if any(value < 1 for value in unique):
            raise ValueError("source 索引必须从 1 开始")
        if len(unique) < 2:
            raise ValueError("调研冲突必须引用至少两个不同来源")
        return unique


class AnchorProfile(BaseModel):
    """Company anchor used for later queries and material attribution."""

    model_config = ConfigDict(extra="forbid")

    official_name: AnchorFieldText
    website_domain: AnchorFieldText
    industry: AnchorFieldText
    location: AnchorFieldText
    confidence: Literal["high", "low"]
    note: Annotated[str, StringConstraints(strip_whitespace=True,
                                           max_length=MAX_ANCHOR_NOTE_CHARS)]


class PlannedQueryOutput(BaseModel):
    """One query produced by planning."""

    model_config = ConfigDict(extra="forbid")

    text: PlanQueryText
    leg: Literal["company", "position"]
    section: Annotated[str, StringConstraints(strip_whitespace=True, max_length=48)]
    kind: Literal["general", "news"] = "general"
    key: bool = False
    language: Literal["zh", "en"] | None = None


class ResearchPlan(BaseModel):
    """Stage-zero anchor and search plan."""

    model_config = ConfigDict(extra="forbid")

    anchor: AnchorProfile
    queries: list[PlannedQueryOutput] = Field(max_length=MAX_PLAN_QUERIES)


class CompanyReport(BaseModel):
    """Source-cited company report cached across roles."""

    model_config = ConfigDict(extra="forbid")

    business: CitedSection
    culture: CitedSection
    recent_news: CitedSection
    interview_style: CitedSection
    source_conflicts: list[ResearchConflict] = Field(
        max_length=MAX_RESEARCH_CONFLICTS,
    )

    @model_validator(mode="after")
    def validate_total_text_budget(self) -> "CompanyReport":
        text_total = sum(len(section.text) for section in (
            self.business, self.culture, self.recent_news, self.interview_style,
        ))
        text_total += sum(len(item.summary) for item in self.source_conflicts)
        if text_total > MAX_RESEARCH_TOTAL_TEXT_CHARS:
            raise ValueError(
                f"公司报告结构化文本合计不能超过 {MAX_RESEARCH_TOTAL_TEXT_CHARS:,} 个字符",
            )
        return self


class ResearchQuestionItem(BaseModel):
    """An observed or inferred assessment item with explicit provenance."""

    model_config = ConfigDict(extra="forbid")

    text: ResearchQuestionText
    category: Literal[
        "hr_motivation", "resume_deep_dive", "behavioral_situational",
        "professional_domain", "business_company", "case_work_sample",
    ]
    provenance: Literal["reported", "inference"]
    sources: list[int] = Field(default_factory=list, max_length=MAX_SECTION_SOURCES)

    @field_validator("sources")
    @classmethod
    def stable_unique(cls, values: list[int]) -> list[int]:
        for value in values:
            if value < 1:
                raise ValueError("source 索引必须从 1 开始")
        return _stable_unique_sources(values)


class PositionReport(BaseModel):
    """Role-level research report stored with application preparation."""

    model_config = ConfigDict(extra="forbid")

    key_takeaways: list[KeyTakeawayText] = Field(max_length=MAX_KEY_TAKEAWAYS)
    interview_process: CitedSection
    experience_highlights: CitedSection
    team_and_work_context: CitedSection
    reported_questions: list[ResearchQuestionItem] = Field(max_length=MAX_TECH_QUESTIONS)
    likely_questions: list[ResearchQuestionItem] = Field(max_length=MAX_RESEARCH_QUESTIONS)
    assessment_focuses: list[ResearchQuestionItem] = Field(max_length=MAX_RESEARCH_QUESTIONS)
    source_conflicts: list[ResearchConflict] = Field(
        max_length=MAX_RESEARCH_CONFLICTS,
    )

    @field_validator("key_takeaways")
    @classmethod
    def stable_unique_texts(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("reported_questions", "likely_questions", "assessment_focuses")
    @classmethod
    def stable_unique_questions(
        cls,
        values: list[ResearchQuestionItem],
        info,
    ) -> list[ResearchQuestionItem]:
        expected = "reported" if info.field_name == "reported_questions" else "inference"
        if any(item.provenance != expected for item in values):
            raise ValueError(f"{info.field_name} provenance must be {expected}")
        if expected == "reported" and any(not item.sources for item in values):
            raise ValueError("reported questions require sources")
        seen: set[str] = set()
        unique: list[ResearchQuestionItem] = []
        for item in values:
            marker = item.text.casefold()
            if marker not in seen:
                seen.add(marker)
                unique.append(item)
        return unique

    @model_validator(mode="after")
    def validate_total_text_budget(self) -> "PositionReport":
        text_total = sum(len(section.text) for section in (
            self.interview_process, self.experience_highlights, self.team_and_work_context,
        ))
        text_total += sum(map(len, self.key_takeaways))
        text_total += sum(len(item.text) for item in self.reported_questions)
        text_total += sum(len(item.text) for item in self.likely_questions)
        text_total += sum(len(item.text) for item in self.assessment_focuses)
        text_total += sum(len(item.summary) for item in self.source_conflicts)
        if text_total > MAX_RESEARCH_TOTAL_TEXT_CHARS:
            raise ValueError(
                f"岗位报告结构化文本合计不能超过 {MAX_RESEARCH_TOTAL_TEXT_CHARS:,} 个字符",
            )
        return self
