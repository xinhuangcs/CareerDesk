"""Strict application research snapshot contracts and pure derivation functions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from ...platform.locale import OutputLocale
from .ai_models import NOT_FOUND


COMPANY_RESEARCH_TTL_DAYS = 14
POSITION_RESEARCH_TTL_DAYS = 14
RESEARCH_SNAPSHOT_VERSION = 3
COMPANY_CACHE_CONTRACT_VERSION = 2

# These versions participate in eligibility/semantic claims. Bump them for relevant
# prompt, schema, or planner changes so old caches become stale at read time.
COMPANY_REPORT_SCHEMA_VERSION = 3
COMPANY_REPORT_PROMPT_VERSION = 2
POSITION_REPORT_SCHEMA_VERSION = 3
POSITION_REPORT_PROMPT_VERSION = 3
RESEARCH_PLANNER_POLICY_VERSION = 1

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_SNAPSHOT_ID_PATTERN = r"^[0-9a-f]{32}$"
_SOURCE_ID_PATTERN = r"^[CP][1-9][0-9]*$"
_MAX_SNAPSHOT_CONFLICTS = 10
_MAX_CONFLICT_SUMMARY_CHARS = 400
_MAX_CONFLICT_SOURCES = 12
_MAIN_SECTIONS: tuple[tuple[str, str], ...] = (
    ("company", "business"),
    ("company", "culture"),
    ("company", "recent_news"),
    ("company", "interview_style"),
    ("position", "interview_process"),
    ("position", "experience_highlights"),
    ("position", "team_and_work_context"),
)
_LEGACY_RESEARCH_KEYS = frozenset(
    {"research", "position_report", "company_report", "anchor", "planner"}
)


def canonical_json_hash(value: Any) -> str:
    """Compute SHA-256 over compact, sorted-key, UTF-8 canonical JSON."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _aliases(values: list[str] | tuple[str, ...] | None) -> list[str]:
    candidates = values if isinstance(values, (list, tuple)) else []
    normalized = {
        value.strip() for value in candidates
        if isinstance(value, str) and value.strip()
    }
    return sorted(normalized, key=lambda value: (value.casefold(), value))


def _default_search_profile(*values: str | None) -> dict:
    sample = " ".join(value or "" for value in values)
    primary = "zh" if re.search(r"[\u3400-\u9fff]", sample) else "en"
    return {
        "version": 1,
        "primary_language": primary,
        "secondary_language": "en" if primary == "zh" else "zh",
        "country": None,
    }


def company_cache_eligibility_claim(
    *,
    company: str,
    aliases: list[str] | tuple[str, ...] | None,
    notes: str | None,
    output_locale: OutputLocale = "zh-CN",
    search_profile: dict | None = None,
) -> dict:
    """Build a stable auditable eligibility claim for company-report caching."""
    return {
        "company": company.strip(),
        "aliases": _aliases(aliases),
        "notes": (notes or "").strip(),
        "content_locale": output_locale,
        "search_profile": search_profile or _default_search_profile(company),
        "policy": {
            "cache_contract": COMPANY_CACHE_CONTRACT_VERSION,
            "company_report_schema": COMPANY_REPORT_SCHEMA_VERSION,
            "company_report_prompt": COMPANY_REPORT_PROMPT_VERSION,
            "planner": RESEARCH_PLANNER_POLICY_VERSION,
        },
    }


def company_cache_eligibility_hash(
    *,
    company: str,
    aliases: list[str] | tuple[str, ...] | None,
    notes: str | None,
    output_locale: OutputLocale = "zh-CN",
    search_profile: dict | None = None,
) -> str:
    return canonical_json_hash(
        company_cache_eligibility_claim(
            company=company,
            aliases=aliases,
            notes=notes,
            output_locale=output_locale,
            search_profile=search_profile,
        )
    )


def research_semantic_claim(
    *,
    company: str,
    aliases: list[str] | tuple[str, ...] | None,
    notes: str | None,
    department: str | None,
    position: str,
    jd_text: str | None,
    output_locale: OutputLocale = "zh-CN",
    search_profile: dict | None = None,
) -> dict:
    """Build the complete external semantic claim bound to an app snapshot."""
    return {
        "company": company.strip(),
        "aliases": _aliases(aliases),
        "notes": (notes or "").strip(),
        "department": _optional_text(department),
        "position": position.strip(),
        "jd_text": _optional_text(jd_text),
        "content_locale": output_locale,
        "search_profile": search_profile or _default_search_profile(company, position, jd_text),
        "policy": {
            "snapshot_schema": RESEARCH_SNAPSHOT_VERSION,
            "company_report_schema": COMPANY_REPORT_SCHEMA_VERSION,
            "company_report_prompt": COMPANY_REPORT_PROMPT_VERSION,
            "position_report_schema": POSITION_REPORT_SCHEMA_VERSION,
            "position_report_prompt": POSITION_REPORT_PROMPT_VERSION,
            "planner": RESEARCH_PLANNER_POLICY_VERSION,
        },
    }


def research_semantic_claim_hash(
    *,
    company: str,
    aliases: list[str] | tuple[str, ...] | None,
    notes: str | None,
    department: str | None,
    position: str,
    jd_text: str | None,
    output_locale: OutputLocale = "zh-CN",
    search_profile: dict | None = None,
) -> str:
    return canonical_json_hash(
        research_semantic_claim(
            company=company,
            aliases=aliases,
            notes=notes,
            department=department,
            position=position,
            jd_text=jd_text,
            output_locale=output_locale,
            search_profile=search_profile,
        )
    )


class ResearchAttempt(BaseModel):
    """Latest research-leg attempt, independent from prep lease and terminal state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    attempt_state: Literal[
        "idle", "pending", "running", "succeeded", "failed", "disabled", "unavailable"
    ]
    generation: str | None = Field(default=None, min_length=1, max_length=200)
    updated_time: str = Field(min_length=1, max_length=64)
    error_code: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("updated_time")
    @classmethod
    def validate_updated_time(cls, value: str) -> str:
        _aware_datetime(value, field_name="updated_time")
        return value

    @model_validator(mode="after")
    def validate_generation(self) -> "ResearchAttempt":
        if self.attempt_state in {"pending", "running"} and self.generation is None:
            raise ValueError("pending/running research attempt 必须绑定 generation")
        return self


class ResearchSource(BaseModel):
    """Source metadata frozen into snapshots without page text or query diagnostics."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    url: str = Field(min_length=1, max_length=4_000)
    site: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=2_000)
    date: str | None = Field(default=None, max_length=64)
    engines: list[str] = Field(default_factory=list, max_length=20)


class ResearchConflict(BaseModel):
    """A frozen contradiction with namespaced references to both sides."""

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=_MAX_CONFLICT_SUMMARY_CHARS,
        ),
    ]
    sources: list[str] = Field(
        min_length=2,
        max_length=_MAX_CONFLICT_SOURCES,
    )

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("调研冲突的 source_id 不能重复")
        if any(re.fullmatch(_SOURCE_ID_PATTERN, value) is None for value in values):
            raise ValueError("调研冲突必须引用 C/P 命名空间的 source_id")
        if len({value[0] for value in values}) != 1:
            raise ValueError("单条调研冲突不能混用公司与岗位来源空间")
        return values


class ResearchSnapshot(BaseModel):
    """Consistent application-level research slice for adaptation and internal users."""

    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot_version: Literal[RESEARCH_SNAPSHOT_VERSION]
    snapshot_id: str = Field(pattern=_SNAPSHOT_ID_PATTERN)
    content_locale: OutputLocale
    search_profile: dict[str, Any]
    semantic_claim_hash: str = Field(pattern=_HASH_PATTERN)
    coverage_quality: Literal["complete", "partial", "insufficient"]
    company_report_hash: str = Field(pattern=_HASH_PATTERN)
    company_report_generated_time: str = Field(min_length=1, max_length=64)
    position_report_hash: str = Field(pattern=_HASH_PATTERN)
    position_report_generated_time: str = Field(min_length=1, max_length=64)
    company_report: dict[str, Any]
    position_report: dict[str, Any]
    company_sources: list[ResearchSource] = Field(max_length=500)
    position_sources: list[ResearchSource] = Field(max_length=500)
    missing_sections: list[str] = Field(max_length=len(_MAIN_SECTIONS))
    coverage_limitations: list[str] = Field(max_length=len(_MAIN_SECTIONS) + 10)
    source_conflicts: list[ResearchConflict] = Field(
        max_length=_MAX_SNAPSHOT_CONFLICTS,
    )
    fresh_until: str = Field(min_length=1, max_length=64)

    @field_validator(
        "company_report_generated_time",
        "position_report_generated_time",
        "fresh_until",
    )
    @classmethod
    def validate_timestamp(cls, value: str, info) -> str:
        _aware_datetime(value, field_name=info.field_name)
        return value

    @model_validator(mode="after")
    def validate_source_namespaces(self) -> "ResearchSnapshot":
        if any(not source.source_id.startswith("C") for source in self.company_sources):
            raise ValueError("company_sources 必须使用 C 命名空间")
        if any(not source.source_id.startswith("P") for source in self.position_sources):
            raise ValueError("position_sources 必须使用 P 命名空间")
        if len({source.source_id for source in self.company_sources}) != len(
            self.company_sources
        ):
            raise ValueError("company_sources source_id 不能重复")
        if len({source.source_id for source in self.position_sources}) != len(
            self.position_sources
        ):
            raise ValueError("position_sources source_id 不能重复")
        source_ids = {
            source.source_id
            for source in [*self.company_sources, *self.position_sources]
        }
        if any(
            source not in source_ids
            for conflict in self.source_conflicts
            for source in conflict.sources
        ):
            raise ValueError("调研冲突包含不存在的 source_id")
        expected_conflicts = []
        for report in (self.company_report, self.position_report):
            raw = report.get("source_conflicts")
            if isinstance(raw, list):
                expected_conflicts.extend(raw)
        if [item.model_dump(mode="json") for item in self.source_conflicts] != expected_conflicts:
            raise ValueError("快照 source_conflicts 必须与双报告中的冲突聚合一致")
        return self


def _aware_datetime(value: str | datetime, *, field_name: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return parsed


def _timestamp(value: str | datetime, *, field_name: str) -> tuple[datetime, str]:
    parsed = _aware_datetime(value, field_name=field_name)
    return parsed, parsed.isoformat()


def _source_records(raw_sources: Any, namespace: Literal["C", "P"]) -> tuple[list[dict], set[int]]:
    records: list[dict] = []
    seen_indexes: set[int] = set()
    if not isinstance(raw_sources, list):
        return records, seen_indexes
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 1:
            continue
        if index in seen_indexes:
            continue
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        engines = raw.get("engines")
        records.append(
            {
                "source_id": f"{namespace}{index}",
                "url": url.strip(),
                "site": raw.get("site") if isinstance(raw.get("site"), str) else "",
                "title": raw.get("title") if isinstance(raw.get("title"), str) else "",
                "date": raw.get("date") if isinstance(raw.get("date"), str) else None,
                "engines": [item for item in (engines or []) if isinstance(item, str)]
                if isinstance(engines, list)
                else [],
            }
        )
        seen_indexes.add(index)
    return records, seen_indexes


def _namespace_report_sources(value: Any, namespace: Literal["C", "P"], valid: set[int]) -> Any:
    if isinstance(value, list):
        return [_namespace_report_sources(item, namespace, valid) for item in value]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for key, child in value.items():
        if key == "sources":
            if not isinstance(child, list):
                normalized[key] = []
                continue
            indexes = [
                item
                for item in child
                if isinstance(item, int) and not isinstance(item, bool) and item in valid
            ]
            normalized[key] = [f"{namespace}{item}" for item in dict.fromkeys(indexes)]
        else:
            normalized[key] = _namespace_report_sources(child, namespace, valid)
    return normalized


def _normalize_report(report: dict, namespace: Literal["C", "P"]) -> tuple[dict, list[dict]]:
    if not isinstance(report, dict):
        raise ValueError("research report 必须是 object")
    raw = copy.deepcopy(report)
    sources, valid_indexes = _source_records(raw.pop("sources", []), namespace)
# Planner/search queries are execution diagnostics, not adaptation-consumable data.
    raw.pop("planner", None)
    return _namespace_report_sources(raw, namespace, valid_indexes), sources


def _section_is_covered(section: Any, valid_source_ids: set[str]) -> bool:
    if not isinstance(section, dict):
        return False
    text = section.get("text")
    if not isinstance(text, str) or not text.strip() or NOT_FOUND in text:
        return False
    sources = section.get("sources")
    return bool(
        isinstance(sources, list)
        and any(isinstance(source, str) and source in valid_source_ids for source in sources)
    )


def build_research_snapshot(
    *,
    company_report: dict,
    position_report: dict,
    semantic_claim: dict,
    company_report_generated_time: str | datetime,
    position_report_generated_time: str | datetime,
    snapshot_id: str | None = None,
) -> dict:
    """Build a strict frozen hashable snapshot from two complete reports."""
    company_time, company_time_text = _timestamp(
        company_report_generated_time, field_name="company_report_generated_time"
    )
    position_time, position_time_text = _timestamp(
        position_report_generated_time, field_name="position_report_generated_time"
    )
    normalized_company, company_sources = _normalize_report(company_report, "C")
    normalized_position, position_sources = _normalize_report(position_report, "P")
    valid_sources = {
        source["source_id"] for source in [*company_sources, *position_sources]
    }

    missing_sections: list[str] = []
    for leg, name in _MAIN_SECTIONS:
        report = normalized_company if leg == "company" else normalized_position
        if not _section_is_covered(report.get(name), valid_sources):
            missing_sections.append(f"{leg}.{name}")
    covered = len(_MAIN_SECTIONS) - len(missing_sections)
    coverage_quality: Literal["complete", "partial", "insufficient"]
    if covered == len(_MAIN_SECTIONS):
        coverage_quality = "complete"
    elif covered == 0:
        coverage_quality = "insufficient"
    else:
        coverage_quality = "partial"

    fresh_until = min(
        company_time + timedelta(days=COMPANY_RESEARCH_TTL_DAYS),
        position_time + timedelta(days=POSITION_RESEARCH_TTL_DAYS),
    )
    payload = {
        "snapshot_version": RESEARCH_SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id or uuid4().hex,
        "content_locale": semantic_claim.get("content_locale", "zh-CN"),
        "search_profile": semantic_claim.get("search_profile", {}),
        "semantic_claim_hash": canonical_json_hash(semantic_claim),
        "coverage_quality": coverage_quality,
        "company_report_hash": canonical_json_hash(normalized_company),
        "company_report_generated_time": company_time_text,
        "position_report_hash": canonical_json_hash(normalized_position),
        "position_report_generated_time": position_time_text,
        "company_report": normalized_company,
        "position_report": normalized_position,
        "company_sources": company_sources,
        "position_sources": position_sources,
        "missing_sections": missing_sections,
        "coverage_limitations": [
            f"{section} 未获得带有效来源的公开信息" for section in missing_sections
        ],
        "source_conflicts": [
            *normalized_company.get("source_conflicts", []),
            *normalized_position.get("source_conflicts", []),
        ],
        "fresh_until": fresh_until.isoformat(),
    }
    return ResearchSnapshot.model_validate(payload).model_dump(mode="json")


def _has_legacy_research(prep: dict) -> bool:
    return any(prep.get(key) is not None for key in _LEGACY_RESEARCH_KEYS)


def derive_research_artifact_state(
    prep: dict | None,
    *,
    current_semantic_claim_hash: str,
    content_locale: OutputLocale = "zh-CN",
    now: datetime | None = None,
) -> dict:
    """Derive eligibility from immutable snapshot, current semantic hash, and time."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now 必须包含时区")
    loaded_prep = prep if isinstance(prep, dict) else {}
    localized = loaded_prep.get("localized")
    localized_entry = (
        localized.get(content_locale)
        if isinstance(localized, dict) and isinstance(localized.get(content_locale), dict)
        else None
    )
    raw_snapshot = (
        localized_entry.get("research_snapshot")
        if localized_entry is not None
        and localized_entry.get("research_snapshot") is not None
        else loaded_prep.get("research_snapshot") if content_locale == "zh-CN" else None
    )
    if raw_snapshot is None:
        return {
            "artifact_state": "legacy" if _has_legacy_research(loaded_prep) else "missing",
            "coverage_quality": None,
            "fresh_until": None,
            "snapshot": None,
        }
    try:
        snapshot_model = ResearchSnapshot.model_validate(raw_snapshot)
    except (ValidationError, TypeError, ValueError):
        return {
            "artifact_state": "legacy" if _has_legacy_research(loaded_prep) else "missing",
            "coverage_quality": None,
            "fresh_until": None,
            "snapshot": None,
        }

    snapshot = snapshot_model.model_dump(mode="json")
    stale = snapshot["semantic_claim_hash"] != current_semantic_claim_hash
    company_time = _aware_datetime(
        snapshot["company_report_generated_time"], field_name="company_report_generated_time"
    )
    position_time = _aware_datetime(
        snapshot["position_report_generated_time"], field_name="position_report_generated_time"
    )
    fresh_until = _aware_datetime(snapshot["fresh_until"], field_name="fresh_until")
    expected_fresh_until = min(
        company_time + timedelta(days=COMPANY_RESEARCH_TTL_DAYS),
        position_time + timedelta(days=POSITION_RESEARCH_TTL_DAYS),
    )
    if company_time > current or position_time > current:
        stale = True
    if current - company_time > timedelta(days=COMPANY_RESEARCH_TTL_DAYS):
        stale = True
    if current - position_time > timedelta(days=POSITION_RESEARCH_TTL_DAYS):
        stale = True
    if fresh_until != expected_fresh_until or current > fresh_until:
        stale = True
    if canonical_json_hash(snapshot["company_report"]) != snapshot["company_report_hash"]:
        stale = True
    if canonical_json_hash(snapshot["position_report"]) != snapshot["position_report_hash"]:
        stale = True

    return {
        "artifact_state": "stale" if stale else "ready",
        "coverage_quality": snapshot["coverage_quality"],
        "fresh_until": snapshot["fresh_until"],
        "snapshot": snapshot,
    }


__all__ = [
    "COMPANY_CACHE_CONTRACT_VERSION",
    "COMPANY_RESEARCH_TTL_DAYS",
    "POSITION_RESEARCH_TTL_DAYS",
    "RESEARCH_SNAPSHOT_VERSION",
    "ResearchAttempt",
    "ResearchConflict",
    "ResearchSnapshot",
    "build_research_snapshot",
    "canonical_json_hash",
    "company_cache_eligibility_claim",
    "company_cache_eligibility_hash",
    "derive_research_artifact_state",
    "research_semantic_claim",
    "research_semantic_claim_hash",
]
