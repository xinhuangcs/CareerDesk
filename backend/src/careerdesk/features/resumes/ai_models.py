"""Bounded structured models for resume parsing."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_RESUME_OUTPUT_LINES = 50
MAX_KNOWLEDGE_POINTS_PER_LINE = 3
MAX_KNOWLEDGE_POINT_CHARS = 100
MAX_RESUME_PARSE_TOTAL_TEXT_CHARS = 20_000

ResumeFamily = Literal["agent_app", "backend", "algorithm", "frontend", "data", "other"]
KnowledgePointText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_KNOWLEDGE_POINT_CHARS,
    ),
]


class ResumeLine(BaseModel):
    """Model-selected trusted input line index and bounded classification."""

    model_config = ConfigDict(extra="forbid")

    # The host fills text by index; the model cannot invent source resume text.
    line_index: int = Field(ge=0, strict=True)
    knowledge_points: list[KnowledgePointText] = Field(
        min_length=1,
        max_length=MAX_KNOWLEDGE_POINTS_PER_LINE,
    )

    @field_validator("knowledge_points")
    @classmethod
    def stable_unique_knowledge_points(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class ResumeParse(BaseModel):
    """Parsed result for one resume."""

    model_config = ConfigDict(extra="forbid")

    family: ResumeFamily
    # Explicit [] means no sufficiently trusted skill points; the field is required.
    lines: list[ResumeLine] = Field(max_length=MAX_RESUME_OUTPUT_LINES)

    @field_validator("lines")
    @classmethod
    def stable_unique_lines(cls, values: list[ResumeLine]) -> list[ResumeLine]:
        """Take a bounded stable union of concepts for one source line."""
        indexes: dict[int, int] = {}
        unique: list[ResumeLine] = []
        for line in values:
            existing_index = indexes.get(line.line_index)
            if existing_index is None:
                indexes[line.line_index] = len(unique)
                unique.append(line)
                continue
            current = unique[existing_index]
            merged_points = list(dict.fromkeys([
                *current.knowledge_points,
                *line.knowledge_points,
            ]))[:MAX_KNOWLEDGE_POINTS_PER_LINE]
            unique[existing_index] = current.model_copy(update={
                "knowledge_points": merged_points,
            })
        return unique

    @model_validator(mode="after")
    def validate_total_text_budget(self) -> "ResumeParse":
        text_total = sum(
            sum(map(len, line.knowledge_points))
            for line in self.lines
        )
        if text_total > MAX_RESUME_PARSE_TOTAL_TEXT_CHARS:
            raise ValueError(
                "简历解析结构化文本合计不能超过 "
                f"{MAX_RESUME_PARSE_TOTAL_TEXT_CHARS:,} 个字符",
            )
        return self
