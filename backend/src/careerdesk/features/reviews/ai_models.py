"""Clean-slate structured extraction contracts for narrated reviews.

A review describes only a happened fact, confirmed current state, and one direct next
action. ``stage`` and ``current_step`` separate coarse state from a specific reached step.
"""

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

from ..applications.public import (
    ApplicationNextAction,
    ApplicationStage,
    StepText,
)

MAX_REVIEW_QUESTIONS = 50
MAX_QUESTION_KNOWLEDGE_POINTS = 3
MAX_REVIEW_FACTORS = 20
MAX_COMPANY_CHARS = 200
MAX_POSITION_CHARS = 300
MAX_CHANNEL_CHARS = 100
MAX_QUESTION_CHARS = 4_000
MAX_KNOWLEDGE_POINT_CHARS = 100
MAX_SUMMARY_CHARS = 2_000
MAX_MOOD_CHARS = 1_000
MAX_FACTOR_CHARS = 200
MAX_REVIEW_TOTAL_TEXT_CHARS = 50_000
MAX_REVIEW_BATCH_ITEMS = 50
MAX_REVIEW_BATCH_TOTAL_TEXT_CHARS = 500_000

_ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

CompanyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COMPANY_CHARS),
]
PositionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_POSITION_CHARS),
]
ChannelText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CHANNEL_CHARS),
]
QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS),
]
KnowledgePointText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_KNOWLEDGE_POINT_CHARS,
    ),
]
SummaryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUMMARY_CHARS),
]
MoodText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MOOD_CHARS),
]
FactorText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FACTOR_CHARS),
]
ISODateText = Annotated[
    str,
    StringConstraints(min_length=10, max_length=10, pattern=_ISO_DATE_PATTERN),
]


def _valid_iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("日期必须是真实的 ISO YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError("日期必须是真实的 ISO YYYY-MM-DD")
    return value


class ExtractedQuestion(BaseModel):
    """One question asked during the reviewed event."""

    model_config = ConfigDict(extra="forbid")

    text: QuestionText
    stuck: bool = False
    knowledge_points: list[KnowledgePointText] = Field(
        default_factory=list,
        max_length=MAX_QUESTION_KNOWLEDGE_POINTS,
    )

    @field_validator("knowledge_points")
    @classmethod
    def stable_unique_knowledge_points(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class ReviewHistoryFact(BaseModel):
    """A fact that happened in this update; future plans never belong here."""

    model_config = ConfigDict(extra="forbid")

    step: StepText | None = None
    date: ISODateText | None = None
    outcome: Literal["passed", "failed", "cancelled"] | None = None
    summary: SummaryText | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        return _valid_iso_date(value)

    @model_validator(mode="after")
    def require_fact(self) -> "ReviewHistoryFact":
        if self.step is None and self.outcome is None and self.summary is None:
            raise ValueError("history 必须包含已经发生的环节、结果或说明")
        return self


class ReviewProjectedState(BaseModel):
    """Current-state delta after the user confirms the fact."""

    model_config = ConfigDict(extra="forbid")

    stage: ApplicationStage | None = None
    current_step: StepText | None = None

    @model_validator(mode="after")
    def require_projection(self) -> "ReviewProjectedState":
        if self.stage is None and self.current_step is None:
            raise ValueError("projected_state 至少要改变阶段或当前环节")
        return self


ReviewNextAction = ApplicationNextAction


class ReviewExtraction(BaseModel):
    """Complete extraction from one narration."""

    model_config = ConfigDict(extra="forbid")

    company: CompanyText | None = None
    position: PositionText | None = None
    channel: ChannelText | None = None
    history: ReviewHistoryFact | None = None
    projected_state: ReviewProjectedState | None = None
    # ``next_action=None`` means "the user did not change the existing plan".
    # Clearing is therefore an explicit command instead of an overloaded null.
    clear_next_action: bool = False
    next_action: ApplicationNextAction | None = None
    questions: list[ExtractedQuestion] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_QUESTIONS,
    )
    mood: MoodText | None = None
    time_of_day: Literal["morning", "afternoon", "evening"] | None = None
    factors: list[FactorText] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_FACTORS,
    )

    @field_validator("questions")
    @classmethod
    def stable_unique_questions(cls, values: list[ExtractedQuestion]) -> list[ExtractedQuestion]:
        indexes: dict[str, int] = {}
        unique: list[ExtractedQuestion] = []
        for question in values:
            index = indexes.get(question.text)
            if index is None:
                indexes[question.text] = len(unique)
                unique.append(question)
                continue
            current = unique[index]
            merged_points = list(dict.fromkeys([
                *current.knowledge_points,
                *question.knowledge_points,
            ]))[:MAX_QUESTION_KNOWLEDGE_POINTS]
            unique[index] = current.model_copy(update={
                "stuck": current.stuck or question.stuck,
                "knowledge_points": merged_points,
            })
        return unique

    @field_validator("factors")
    @classmethod
    def stable_unique_factors(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_total_text_budget(self) -> "ReviewExtraction":
        if (
            self.history is None
            and self.projected_state is None
            and not self.clear_next_action
            and self.next_action is None
        ):
            raise ValueError("复盘必须包含已发生事实、当前状态变化或下一步安排")
        history = self.history
        projected = self.projected_state
        next_action = self.next_action
        if self.clear_next_action and next_action is not None:
            raise ValueError("clear_next_action 与 next_action 不能同时设置")
        if (
            projected is not None
            and projected.stage in {"withdrawn", "rejected"}
            and next_action is not None
        ):
            raise ValueError("已挂或不再跟进的岗位不能同时安排下一步")
        text_total = sum(len(value or "") for value in (
            self.company,
            self.position,
            self.channel,
            history.step if history else None,
            history.date if history else None,
            history.summary if history else None,
            projected.current_step if projected else None,
            next_action.step if next_action else None,
            next_action.date if next_action else None,
            next_action.time if next_action else None,
            next_action.note if next_action else None,
            self.mood,
        ))
        text_total += sum(len(factor) for factor in self.factors)
        for question in self.questions:
            text_total += len(question.text)
            text_total += sum(len(point) for point in question.knowledge_points)
        if text_total > MAX_REVIEW_TOTAL_TEXT_CHARS:
            raise ValueError(
                f"复盘结构化文本合计不能超过 {MAX_REVIEW_TOTAL_TEXT_CHARS:,} 个字符",
            )
        return self


def infer_completed_next_action_clear(
    extraction: ReviewExtraction,
    current_next_action: ApplicationNextAction | None,
) -> ReviewExtraction:
    """Turn a matching completed step into an explicit, reviewable plan clear.

    The extractor does not know the live application projection.  This inference
    therefore runs only after the target and its current next action are frozen.
    Explicit set/clear choices always win; unrelated completed steps preserve the
    existing plan.
    """
    if (
        extraction.clear_next_action
        or extraction.next_action is not None
        or current_next_action is None
        or extraction.history is None
        or extraction.history.step is None
    ):
        return extraction
    completed_step = " ".join(extraction.history.step.split()).casefold()
    planned_step = " ".join(current_next_action.step.split()).casefold()
    if completed_step != planned_step:
        return extraction
    return extraction.model_copy(update={"clear_next_action": True})


class ReviewBatchItem(BaseModel):
    """One independently confirmable role update."""

    model_config = ConfigDict(extra="forbid")

    source_text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REVIEW_TOTAL_TEXT_CHARS),
    ]
    extraction: ReviewExtraction


class ReviewBatchIdentity(BaseModel):
    """One source-grounded role identity discovered before full extraction."""

    model_config = ConfigDict(extra="forbid")

    source_text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REVIEW_TOTAL_TEXT_CHARS),
    ]
    company: CompanyText | None = None
    position: PositionText | None = None

    @model_validator(mode="after")
    def require_identity(self) -> "ReviewBatchIdentity":
        if self.company is None and self.position is None:
            raise ValueError("批量复盘岗位清单至少要包含公司或岗位")
        return self


class ReviewBatchIdentityManifest(BaseModel):
    """Bounded identity manifest used as the batch completeness contract."""

    model_config = ConfigDict(extra="forbid")

    items: list[ReviewBatchIdentity] = Field(
        min_length=1,
        max_length=MAX_REVIEW_BATCH_ITEMS,
    )

    @model_validator(mode="after")
    def validate_manifest_bounds(self) -> "ReviewBatchIdentityManifest":
        identities = [(item.company, item.position) for item in self.items]
        if len(identities) != len(set(identities)):
            raise ValueError("批量复盘岗位清单不能重复同一身份")
        total_chars = sum(len(item.model_dump_json()) for item in self.items)
        if total_chars > MAX_REVIEW_BATCH_TOTAL_TEXT_CHARS:
            raise ValueError(
                "批量复盘岗位清单结构化文本合计不能超过 "
                f"{MAX_REVIEW_BATCH_TOTAL_TEXT_CHARS:,} 个字符",
            )
        return self


class ReviewBatchExtraction(BaseModel):
    """Bounded role-update batch split from one message."""

    model_config = ConfigDict(extra="forbid")

    items: list[ReviewBatchItem] = Field(min_length=1, max_length=MAX_REVIEW_BATCH_ITEMS)

    @model_validator(mode="after")
    def validate_batch_bounds(self) -> "ReviewBatchExtraction":
        sources = [item.source_text for item in self.items]
        if len(sources) != len(set(sources)):
            raise ValueError("批量复盘 source_text 必须唯一")
        total_chars = sum(len(item.model_dump_json()) for item in self.items)
        if total_chars > MAX_REVIEW_BATCH_TOTAL_TEXT_CHARS:
            raise ValueError(
                "批量复盘结构化文本合计不能超过 "
                f"{MAX_REVIEW_BATCH_TOTAL_TEXT_CHARS:,} 个字符",
            )
        return self


__all__ = [
    "ApplicationStage",
    "CompanyText",
    "ExtractedQuestion",
    "ISODateText",
    "PositionText",
    "ReviewBatchExtraction",
    "ReviewBatchIdentity",
    "ReviewBatchIdentityManifest",
    "ReviewBatchItem",
    "ReviewExtraction",
    "ReviewHistoryFact",
    "infer_completed_next_action_clear",
    "ReviewNextAction",
    "ReviewProjectedState",
    "StepText",
]
