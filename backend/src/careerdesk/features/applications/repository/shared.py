"""Shared application-stage and timeline helpers."""

import json
from datetime import date, datetime, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo


BOARD_STAGES = [
    "backlog", "applied", "written_test", "interviewing", "offer",
    "pooled", "withdrawn", "rejected",
]
STAGE_LABELS = {
    "backlog": "待定",
    "applied": "已投递",
    "written_test": "笔试中",
    "interviewing": "面试中",
    "offer": "Offer",
    "pooled": "泡池子",
    "withdrawn": "不再跟进",
    "rejected": "已挂",
}
CLOSED_STAGES = frozenset({"rejected", "withdrawn"})
ACTIVE_STAGES = frozenset({"backlog", "applied", "written_test", "interviewing", "offer"})


def normalize_optional_text(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise ValueError(f"文本不能超过 {limit} 个字符")
    return normalized


def validate_iso_date(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是真实的 YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{label}必须是真实的 YYYY-MM-DD")
    return value


def timeline_entry_display_time(created_time: str, timezone_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(created_time)
    except (TypeError, ValueError):
        return "时间未知"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")


def timeline_entry_snapshot_fingerprint(
    *,
    created_time: str,
    step: str | None,
    occurred_date: str | None,
    outcome: str | None,
    summary: str | None,
    from_stage: str,
    from_step: str | None,
    to_stage: str,
    to_step: str | None,
    source: str,
) -> str:
    encoded = json.dumps(
        {
            "created_time": created_time,
            "step": step,
            "occurred_date": occurred_date,
            "outcome": outcome,
            "summary": summary,
            "from_stage": from_stage,
            "from_step": from_step,
            "to_stage": to_stage,
            "to_step": to_step,
            "source": source,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


class IntakeOperationNotFound(LookupError):
    """Trusted batch import is absent or belongs to another user."""


class IntakeOperationConflict(RuntimeError):
    """Operation is terminal, expired, or replayed with a different command."""


class IntakeOperationInvalidSelection(ValueError):
    """User-selected preview row number is invalid."""


class TimelineMutationConflict(RuntimeError):
    """Another request changed Timeline; this command cannot overwrite newer state."""
