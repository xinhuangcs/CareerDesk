"""Deterministic resume policies and input boundaries."""

import re
import hashlib

from .ai_models import (
    MAX_KNOWLEDGE_POINTS_PER_LINE,
    MAX_KNOWLEDGE_POINT_CHARS,
    MAX_RESUME_OUTPUT_LINES,
)


# Typical resumes are far smaller; this generous bound still blocks pathological model input.
MAX_RESUME_TEXT_CHARS = 200_000
MAX_RESUME_SOURCE_LINE_CHARS = 4_000
# Full-text projections persist as JSON. Reject one-character-per-line pathologies while
# allowing ordinary multi-page resumes far beyond the old 50-point limit; never truncate.
MAX_RESUME_SOURCE_SEGMENTS = 2_000

# A knowledge box at this threshold is considered stable preparation.
STEADY_BOX = 2


def canonicalize_resume_text(text: str) -> str:
    """Canonicalize line endings without trimming or changing Unicode."""
    if not isinstance(text, str):
        raise ValueError("简历文本格式无效")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_resume_text(text: str) -> str:
    """Enforce the shared text bound before any resume model call."""
    text = canonicalize_resume_text(text)
    if not text.strip():
        raise ValueError("简历文本不能为空")
    if len(text) > MAX_RESUME_TEXT_CHARS:
        raise ValueError(f"简历文本不能超过 {MAX_RESUME_TEXT_CHARS:,} 个字符")
    return text


def resume_content_hash(text: str) -> str:
    return hashlib.sha256(validate_resume_text(text).encode("utf-8")).hexdigest()


def segment_resume_text(text: str) -> list[dict]:
    """Build a lossless, bounded span projection over canonical text."""
    text = validate_resume_text(text)
    segments: list[dict] = []
    cursor = 0
    for physical in text.splitlines(keepends=True):
        content_end = cursor + len(physical.rstrip("\n"))
        start = cursor
        while start < content_end:
            end = min(start + MAX_RESUME_SOURCE_LINE_CHARS, content_end)
            segments.append({
                "id": f"R{len(segments) + 1}",
                "start": start,
                "end": end,
                "text": text[start:end],
            })
            start = end
        cursor += len(physical)
    if cursor < len(text):
        segments.append({
            "id": f"R{len(segments) + 1}",
            "start": cursor,
            "end": len(text),
            "text": text[cursor:],
        })
    if len(segments) > MAX_RESUME_SOURCE_SEGMENTS:
        raise ValueError(f"简历可见段落不能超过 {MAX_RESUME_SOURCE_SEGMENTS:,} 段")
    if not segments:
        raise ValueError("简历文本不能为空")
    return segments


def normalize_resume_line(text: str) -> str:
    """Remove whitespace for stable comparison of model echoes to source text."""
    return re.sub(r"\s+", "", text)


def resume_analysis_lines(raw_lines):
    """Validate the full persisted projection, then select annotated segments.

    New ``lines_json`` stores all visible text. Empty concepts mean persist-only, not AI
    analysis. Filtering follows full validation so corrupt sentinels cannot be hidden;
    legacy all-point structures remain compatible.
    """
    if not isinstance(raw_lines, list):
        raise ValueError("简历要点格式无效，请重新上传并更新该简历后重试")
    if len(raw_lines) > MAX_RESUME_SOURCE_SEGMENTS:
        raise ValueError("简历可见段落数量无效，请重新上传并更新该简历后重试")
    analysis: list[dict] = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ValueError("简历要点格式无效，请重新上传并更新该简历后重试")
        text = raw.get("text")
        knowledge_points = raw.get("knowledge_points")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.strip()) > MAX_RESUME_SOURCE_LINE_CHARS
            or not isinstance(knowledge_points, list)
            or any(
                not isinstance(point, str)
                or not point.strip()
                or len(point.strip()) > MAX_KNOWLEDGE_POINT_CHARS
                for point in knowledge_points
            )
        ):
            raise ValueError("简历要点格式无效，请重新上传并更新该简历后重试")
        unique_points = list(dict.fromkeys(point.strip() for point in knowledge_points))
        if len(unique_points) > MAX_KNOWLEDGE_POINTS_PER_LINE:
            raise ValueError("简历要点格式无效，请重新上传并更新该简历后重试")
        canonical = {
            "text": text.strip(),
            "knowledge_points": unique_points,
        }
        if canonical["knowledge_points"] == []:
            continue
        analysis.append(canonical)
    if len(analysis) > MAX_RESUME_OUTPUT_LINES:
        raise ValueError(
            f"简历要点超过 {MAX_RESUME_OUTPUT_LINES} 条，请更新该简历后重试"
        )
    return analysis
