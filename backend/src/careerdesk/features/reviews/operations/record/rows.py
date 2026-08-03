"""Validation, canonical digests, turn classification and bounded row reads."""

from __future__ import annotations

import json
from hashlib import sha256
from sqlite3 import Connection
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .....platform.database import loads_json
from ..record_models import (
    MAX_REVIEW_RECORD_COMBINED_CHARS,
    MAX_REVIEW_RECORD_SOURCE_CHARS,
    MAX_REVIEW_RECORD_SUPPLEMENTS,
    REVIEW_RECORD_CONTRACT_VERSION,
    ReviewRecordProposal,
)
from .errors import ReviewRecordOperationConflict, _UnsafeRecordDependency


MAX_REVIEW_RECORD_PERSISTED_JSON_CHARS = 200_000
MAX_AMBIGUOUS_POSITION_OPTIONS = 20
MAX_TURN_OPERATION_CANDIDATES = 64
MAX_TURN_OPERATION_JSON_CHARS = 500_000
UNKNOWN_POSITION = "未注明岗位"

_OPERATION_COLUMNS = (
    "id, operation_id, state, created_time, processed_time, extraction_json, "
    "derivation_json, parent_journal_id, revision, kind"
)
_TURN_OPERATION_FAMILIES = frozenset({
    "application_update",
    "preference_update",
    "review_timeline_entry_edit",
    "review_record",
})


def _canonical_uuid(value: str | UUID, *, label: str) -> str:
    try:
        canonical = str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须是 UUID") from error
    if str(value) != canonical:
        raise ValueError(f"{label} 必须是规范的小写 UUID")
    return canonical


def _validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id or len(user_id) > 512:
        raise ValueError("user_id 必须是有界非空字符串")
    return user_id


def _validate_source_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("text 必须是字符串")
    if not text.strip():
        raise ValueError("text 不能为空")
    if len(text) > MAX_REVIEW_RECORD_SOURCE_CHARS:
        raise ValueError(
            f"单次复盘或补充不能超过 {MAX_REVIEW_RECORD_SOURCE_CHARS:,} 个字符",
        )
    return text


def _canonical_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_REVIEW_RECORD_PERSISTED_JSON_CHARS:
        raise _UnsafeRecordDependency("复盘操作 JSON 超过安全上限")
    return encoded


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _request_digest(
    client_turn_id: str,
    text: str,
    review_reference: str | None,
) -> str:
    return _digest({
        "client_turn_id": client_turn_id,
        "mode": "supplement" if review_reference is not None else "initial",
        "review_reference": review_reference,
        "text": text,
        "version": REVIEW_RECORD_CONTRACT_VERSION,
    })


def _object_json(raw: str | None, label: str) -> dict:
    if not isinstance(raw, str) or len(raw) > MAX_REVIEW_RECORD_PERSISTED_JSON_CHARS:
        raise ReviewRecordOperationConflict(f"{label} 不符合安全契约")
    loaded = loads_json(raw, None)
    if not isinstance(loaded, dict):
        raise ReviewRecordOperationConflict(f"{label} 不是 JSON object")
    return loaded


def _proposal(raw: str | None) -> ReviewRecordProposal:
    try:
        return ReviewRecordProposal.model_validate(_object_json(raw, "复盘操作命令"))
    except (TypeError, ValueError, ValidationError) as error:
        raise ReviewRecordOperationConflict("复盘操作命令已损坏") from error


def _proposal_digest(proposal: ReviewRecordProposal) -> str:
    return _digest(proposal.model_dump(mode="json"))


def _persisted_uuid(value, *, label: str) -> str:
    canonical = None
    if isinstance(value, str):
        try:
            canonical = str(UUID(value))
        except (AttributeError, TypeError, ValueError):
            pass
    if not isinstance(value, str) or value != canonical:
        raise ReviewRecordOperationConflict(f"同轮 operation {label} 身份已损坏")
    return value


def _turn_operation_json(raw: str | None) -> dict | None:
    if not isinstance(raw, str) or len(raw) > MAX_TURN_OPERATION_JSON_CHARS:
        return None
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _classify_turn_candidate(
    *,
    operation_id,
    kind,
    extraction_json,
    derivation_json,
    client_turn_id: str,
) -> str:
    _persisted_uuid(operation_id, label="operation_id")
    if kind != "correction":
        raise ReviewRecordOperationConflict("同轮 operation kind 身份已损坏")

    extraction = _turn_operation_json(extraction_json)
    derivation = _turn_operation_json(derivation_json)
    if (
        extraction is None
        or derivation is None
        or set(derivation) != {"operation"}
        or not isinstance(derivation.get("operation"), dict)
    ):
        raise ReviewRecordOperationConflict("同轮 operation receipt 身份已损坏")
    operation = derivation["operation"]
    extraction_family = extraction.get("operation_type")
    derivation_family_keys = {"type", "operation_type"} & set(operation)
    if len(derivation_family_keys) != 1:
        raise ReviewRecordOperationConflict("同轮 operation family 身份已损坏")
    derivation_family_key = next(iter(derivation_family_keys))
    derivation_family = operation.get(derivation_family_key)
    if (
        not isinstance(extraction_family, str)
        or not isinstance(derivation_family, str)
        or extraction_family != derivation_family
        or extraction_family not in _TURN_OPERATION_FAMILIES
        or (
            extraction_family == "preference_update"
            and derivation_family_key != "operation_type"
        )
        or (
            extraction_family != "preference_update"
            and derivation_family_key != "type"
        )
    ):
        raise ReviewRecordOperationConflict("同轮 operation family 身份已损坏")

    extraction_turn = _persisted_uuid(
        extraction.get("client_turn_id"),
        label="client_turn_id",
    )
    derivation_turn = _persisted_uuid(
        operation.get("client_turn_id"),
        label="client_turn_id",
    )
    if extraction_turn != derivation_turn or extraction_turn != client_turn_id:
        raise ReviewRecordOperationConflict("同轮 operation turn 身份已损坏")
    return extraction_family


def _turn_candidate_family(row, client_turn_id: str) -> str:
    return _classify_turn_candidate(
        operation_id=row[1],
        kind=row[9],
        extraction_json=row[5],
        derivation_json=row[6],
        client_turn_id=client_turn_id,
    )


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
        "AND operation_id = ?",
        (user_id, operation_id),
    ).fetchone()


def _bounded_combined(original: str, supplements: list[str]) -> str:
    if not isinstance(original, str) or len(original) > MAX_REVIEW_RECORD_SOURCE_CHARS:
        raise _UnsafeRecordDependency("原始复盘文本超过安全上限或类型损坏")
    if len(supplements) > MAX_REVIEW_RECORD_SUPPLEMENTS:
        raise _UnsafeRecordDependency("复盘补充次数超过安全上限")
    if any(not isinstance(item, str) or len(item) > MAX_REVIEW_RECORD_SOURCE_CHARS
           for item in supplements):
        raise _UnsafeRecordDependency("复盘补充文本超过安全上限或类型损坏")
    combined = "\n".join([original, *supplements])
    if len(combined) > MAX_REVIEW_RECORD_COMBINED_CHARS:
        raise _UnsafeRecordDependency("复盘与补充的合并文本超过安全上限")
    return combined


def _supplement_rows(
    conn: Connection,
    user_id: str,
    target_journal_id: int,
    *,
    through_source_journal_id: int | None = None,
) -> list:
    through = "" if through_source_journal_id is None else "AND id <= ? "
    parameters = [user_id, target_journal_id]
    if through_source_journal_id is not None:
        parameters.append(through_source_journal_id)
    query = (
        "SELECT id, content FROM journal WHERE user_id = ? AND kind = 'correction' "
        "AND parent_journal_id = ? AND operation_id IS NULL AND state = 'applied' "
        "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.source_type') = 'review_supplement' "
        + through
        + "ORDER BY created_time, id LIMIT ?"
    )
    rows = conn.execute(
        query,
        (*parameters, MAX_REVIEW_RECORD_SUPPLEMENTS + 1),
    ).fetchall()
    if len(rows) > MAX_REVIEW_RECORD_SUPPLEMENTS:
        raise _UnsafeRecordDependency("复盘补充次数超过安全上限")
    return rows
