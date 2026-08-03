"""Structured-output contracts and proposal normalization for batch applications."""

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

from ...platform.database import squash_whitespace

MAX_BATCH_POSITIONS = 200
MAX_COMPANY_CHARS = 200
MAX_POSITION_CHARS = 300
MAX_DEPARTMENT_CHARS = 200
MAX_CHANNEL_CHARS = 100
MAX_BATCH_JD_TEXT_CHARS = 50_000
MAX_BATCH_TOTAL_TEXT_CHARS = 100_000
MAX_SKILLS = 8
MAX_SKILL_CHARS = 100
MAX_HIGHLIGHTS = 20
MAX_HIGHLIGHT_CHARS = 500
MAX_STEP_CHARS = 300
MAX_NEXT_NOTE_CHARS = 2_000
MAX_PAUSE_REASON_CHARS = 1_000
MAX_APPLICATION_NOTE_CHARS = 2_000
INTAKE_CONTRACT_VERSION = 3

_ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_SCALAR_MERGE_FIELDS = (
    "department",
    "channel",
    "stage",
    "current_step",
    "applied_date",
    "next_action",
    "jd_text",
    "jd_source_start",
    "jd_source_end",
    "pause_reason",
    "application_note",
    "priority",
)

CompanyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COMPANY_CHARS),
]
PositionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_POSITION_CHARS),
]
DepartmentText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_DEPARTMENT_CHARS),
]
ChannelText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CHANNEL_CHARS),
]
JDText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_BATCH_JD_TEXT_CHARS),
]
SkillText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SKILL_CHARS),
]
HighlightText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_HIGHLIGHT_CHARS),
]
ISODateText = Annotated[str, StringConstraints(pattern=_ISO_DATE_PATTERN)]
StepText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_STEP_CHARS),
]
NextNoteText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NEXT_NOTE_CHARS),
]
DependencyFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
ApplicationStage = Literal[
    "backlog", "applied", "written_test", "interviewing", "offer",
    "withdrawn", "rejected", "pooled",
]
ApplicationPriority = Literal["high", "medium", "low"]


class ApplicationNextAction(BaseModel):
    """Represent the single immediate successor for an application."""

    model_config = ConfigDict(extra="forbid")

    stage: ApplicationStage
    step: StepText
    date: ISODateText | None = None
    time: Annotated[str, StringConstraints(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] | None = None
    note: NextNoteText | None = None

    @field_validator("date")
    @classmethod
    def validate_real_date(cls, value: str | None) -> str | None:
        """Reject impossible calendar dates."""
        return ParsedPosition.validate_real_iso_date(value)

    @model_validator(mode="after")
    def require_date_for_time(self) -> "ApplicationNextAction":
        """Keep an optional clock time anchored to a calendar date."""
        if self.time is not None and self.date is None:
            raise ValueError("下一步时间必须同时填写日期")
        return self


class ParsedPosition(BaseModel):
    """One bounded application extracted from pasted text."""

    model_config = ConfigDict(extra="forbid")

    company: CompanyText
    position: PositionText
    department: DepartmentText | None = None
    channel: ChannelText | None = None
    stage: ApplicationStage | None = None
    current_step: StepText | None = None
    applied_date: ISODateText | None = None
    pause_reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1,
                               max_length=MAX_PAUSE_REASON_CHARS)
    ] | None = None
    next_action: ApplicationNextAction | None = None
    jd_text: JDText | None = None
    jd_source_start: int | None = Field(default=None, ge=0)
    jd_source_end: int | None = Field(default=None, gt=0)
    source_kind: Literal["text", "workbook"] = "text"
    source_row: str | None = Field(default=None, max_length=300)
    application_note: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1,
                               max_length=MAX_APPLICATION_NOTE_CHARS)
    ] | None = None
    priority: ApplicationPriority | None = None
    skills: list[SkillText] = Field(default_factory=list, max_length=MAX_SKILLS)
    highlights: list[HighlightText] = Field(default_factory=list, max_length=MAX_HIGHLIGHTS)

    @field_validator("applied_date")
    @classmethod
    def validate_real_iso_date(cls, value: str | None) -> str | None:
        """Let JSON Schema bound shape, then reject impossible calendar dates."""
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("日期必须是真实的 ISO YYYY-MM-DD") from error
        if parsed.isoformat() != value:
            raise ValueError("日期必须是真实的 ISO YYYY-MM-DD")
        return value

    @field_validator("jd_text")
    @classmethod
    def normalize_blank_jd(cls, value: str | None) -> str | None:
        """Preserve genuine JD source text; whitespace-only content is invalid."""
        if value is not None and not value.strip():
            return None
        return value

    @field_validator("skills", "highlights")
    @classmethod
    def stable_unique_items(cls, values: list[str]) -> list[str]:
        """Deduplicate in order so repeated model keywords do not crowd previews."""
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_status_date_consistency(self) -> "ParsedPosition":
        if self.stage == "backlog" and self.applied_date is not None:
            raise ValueError("backlog 岗位不能同时带投递日期")
        if self.stage != "pooled" and self.pause_reason is not None:
            raise ValueError("只有泡池子阶段可以填写暂停原因")
        if self.source_kind == "workbook":
            if not self.source_row:
                raise ValueError("表格岗位必须保留来源行")
            if self.jd_source_start is not None or self.jd_source_end is not None:
                raise ValueError("表格岗位不得伪造文本 JD span")
        elif self.source_row is not None:
            raise ValueError("文本岗位不得携带表格来源行")
        if self.jd_text is None:
            if self.jd_source_start is not None or self.jd_source_end is not None:
                raise ValueError("没有 JD 原文时不得提供 JD span")
        elif self.source_kind == "text" and (
            self.jd_source_start is None
            or self.jd_source_end is None
            or self.jd_source_end <= self.jd_source_start
        ):
            raise ValueError("JD 原文必须附带有效的 source span")
        return self


class BatchParse(BaseModel):
    """Complete bounded structured output for one batch paste."""

    model_config = ConfigDict(extra="forbid")

    positions: list[ParsedPosition] = Field(
        default_factory=list,
        max_length=MAX_BATCH_POSITIONS,
    )

    @model_validator(mode="after")
    def validate_total_text_budgets(self) -> "BatchParse":
        jd_total = sum(len(position.jd_text or "") for position in self.positions)
        if jd_total > MAX_BATCH_JD_TEXT_CHARS:
            raise ValueError(
                f"批量岗位 JD 原文合计不能超过 {MAX_BATCH_JD_TEXT_CHARS:,} 个字符",
            )
        text_total = 0
        for position in self.positions:
            text_total += sum(len(value or "") for value in (
                position.company,
                position.position,
                position.department,
                position.channel,
                position.stage,
                position.current_step,
                position.applied_date,
                position.pause_reason,
                position.jd_text,
                position.source_row,
                position.application_note,
                position.priority,
            ))
            if position.next_action is not None:
                text_total += sum(len(value or "") for value in (
                    position.next_action.stage,
                    position.next_action.step,
                    position.next_action.date,
                    position.next_action.time,
                    position.next_action.note,
                ))
            text_total += sum(map(len, position.skills))
            text_total += sum(map(len, position.highlights))
        if text_total > MAX_BATCH_TOTAL_TEXT_CHARS:
            raise ValueError(
                f"批量岗位结构化文本合计不能超过 {MAX_BATCH_TOTAL_TEXT_CHARS:,} 个字符",
            )
        return self


class CreateBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["create"]
    must_be_absent: Literal[True]


class UpdateBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["update"]
    application_id: int = Field(gt=0)
    dependency_fingerprint: DependencyFingerprint


IntakeBinding = Annotated[CreateBinding | UpdateBinding, Field(discriminator="mode")]


class IntakeEffect(BaseModel):
    """Exact visible application-row effect after user approval."""

    model_config = ConfigDict(extra="forbid")

    company: CompanyText
    position: PositionText
    department: DepartmentText | None = None
    channel: ChannelText | None = None
    jd_text: JDText | None = None
    skills: list[SkillText] = Field(default_factory=list, max_length=MAX_SKILLS)
    highlights: list[HighlightText] = Field(default_factory=list, max_length=MAX_HIGHLIGHTS)
    stage: ApplicationStage
    current_step: StepText | None = None
    applied_date: ISODateText | None = None
    pause_reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1,
                               max_length=MAX_PAUSE_REASON_CHARS)
    ] | None = None
    next_action: ApplicationNextAction | None = None
    application_note: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1,
                               max_length=MAX_APPLICATION_NOTE_CHARS)
    ] | None = None
    priority: ApplicationPriority | None = None

    @field_validator("applied_date")
    @classmethod
    def validate_real_iso_date(cls, value: str | None) -> str | None:
        return ParsedPosition.validate_real_iso_date(value)

    @field_validator("skills", "highlights")
    @classmethod
    def stable_unique_items(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

class IntakeFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invalidate_prep: bool
    add_applied_entry: bool
    clear_next_action: bool


class IntakeProposalPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ParsedPosition
    binding: IntakeBinding
    effect: IntakeEffect
    flags: IntakeFlags

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> "IntakeProposalPosition":
        # Existing matches retain stored text; source may differ only in whitespace.
        if (
            squash_whitespace(self.source.company),
            squash_whitespace(self.source.position),
        ) != (squash_whitespace(self.effect.company), squash_whitespace(self.effect.position)):
            raise ValueError("proposal source/effect 自然键不一致")
        if self.source.stage is not None and self.effect.stage != self.source.stage:
            raise ValueError("proposal 没有保留原文明确给出的岗位状态")
        if self.flags.add_applied_entry and self.effect.applied_date is None:
            raise ValueError("add_applied_entry 必须有 applied_date")
        if self.flags.clear_next_action and self.effect.next_action is not None:
            raise ValueError("clear_next_action 必须产生空 next_action")
        if self.binding.mode == "create" and self.flags.invalidate_prep:
            raise ValueError("create 不得标记 invalidate_prep")
        return self


class IntakeProposal(BaseModel):
    """Immutable import plan that is never recomputed after journaling."""

    model_config = ConfigDict(extra="forbid")

    intake_contract_version: Literal[INTAKE_CONTRACT_VERSION]
    positions: list[IntakeProposalPosition] = Field(
        min_length=1,
        max_length=MAX_BATCH_POSITIONS,
    )
    source_rows: int = Field(default=0, ge=0, le=10_000)
    skipped_rows: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_contract(self) -> "IntakeProposal":
        # Reapply structured-output text budgets because persisted JSON is untrusted.
        BatchParse(positions=[position.source for position in self.positions])
        keys = [(position.effect.company, position.effect.position)
                for position in self.positions]
        if len(keys) != len(set(keys)):
            raise ValueError("proposal 包含重复的公司+岗位")
        update_ids = [
            position.binding.application_id
            for position in self.positions
            if position.binding.mode == "update"
        ]
        if len(update_ids) != len(set(update_ids)):
            raise ValueError("proposal 的多个岗位绑定到了同一 application")
        return self


def public_intake_position(position: IntakeProposalPosition) -> dict:
    """Expose planned effects/flags without bindings or UI-side rule recomputation."""
    return {
        "mode": position.binding.mode,
        **position.effect.model_dump(),
        "flags": position.flags.model_dump(),
        "already_exists": position.binding.mode == "update",
    }


def _append_stable_unique(target: list[str], additions: list[str], *, limit: int) -> None:
    seen = set(target)
    for value in additions:
        if value in seen:
            continue
        if len(target) >= limit:
            return
        target.append(value)
        seen.add(value)


def merge_duplicate_positions(positions: list[ParsedPosition]) -> list[ParsedPosition]:
    """Merge model duplicates by exact natural key for consistent row counts.

    First appearance determines order; scalars keep the first nonempty value, while
    lists deduplicate in order under their hard bounds.
    """
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for position in positions:
        item = position.model_dump()
        key = (squash_whitespace(position.company), squash_whitespace(position.position))
        current = merged.get(key)
        if current is None:
            item["skills"] = list(dict.fromkeys(item["skills"]))
            item["highlights"] = list(dict.fromkeys(item["highlights"]))
            merged[key] = item
            order.append(key)
            continue
        for field in _SCALAR_MERGE_FIELDS:
            if current[field] is None and item[field] is not None:
                current[field] = item[field]
        _append_stable_unique(current["skills"], item["skills"], limit=MAX_SKILLS)
        _append_stable_unique(
            current["highlights"],
            item["highlights"],
            limit=MAX_HIGHLIGHTS,
        )
    return [ParsedPosition.model_validate(merged[key]) for key in order]
