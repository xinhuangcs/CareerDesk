"""Validation and target resolution for Review timeline-entry edits."""

from __future__ import annotations

import hashlib
import json
from sqlite3 import Connection
from uuid import UUID

from pydantic import ValidationError

from .....platform.database import squash_whitespace
from ....journal import public as journal
from ..edit_models import ReviewTimelineEntryEditCommand
from .bundle import _load_bundle
from .errors import (
    ReviewTimelineEntryEditOperationConflict,
    _EditTargetMissing,
    _UnsafeEditDependency,
)

UNKNOWN_POSITION = "未注明岗位"
MAX_SELECTOR_OPTIONS = 20
MAX_OPERATIONS_PER_TURN = 20
MAX_TURN_OPERATION_CANDIDATES = 100
MAX_PERSISTED_JSON_CHARS = 200_000
MAX_DEPENDENCY_JSON_CHARS = 100_000
MAX_REVIEW_SOURCE_CHARS = 50_000
_TURN_OPERATION_FAMILIES = frozenset({
    "application_update",
    "preference_update",
    "review_timeline_entry_edit",
    "review_record",
})


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _canonical_uuid(value, *, label: str) -> str:
    try:
        canonical = str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须是 UUID") from error
    if isinstance(value, str) and value != canonical:
        raise ValueError(f"{label} 必须是规范小写 UUID")
    return canonical


def _turn_json(raw: str | None) -> dict | None:
    if not isinstance(raw, str) or len(raw) > MAX_PERSISTED_JSON_CHARS:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _persisted_uuid(value, *, label: str) -> str:
    try:
        canonical = str(UUID(value)) if isinstance(value, str) else None
    except (AttributeError, TypeError, ValueError):
        canonical = None
    if value != canonical:
        raise ReviewTimelineEntryEditOperationConflict(
            f"同轮 operation {label} 身份已损坏",
        )
    return canonical


def _classify_turn_candidate(candidate, client_turn_id: str) -> str:
    _persisted_uuid(candidate.operation_id, label="operation_id")
    if candidate.kind != "correction":
        raise ReviewTimelineEntryEditOperationConflict("同轮 operation kind 身份已损坏")
    extraction = _turn_json(candidate.extraction_json)
    derivation = _turn_json(candidate.derivation_json)
    operation = derivation.get("operation") if derivation is not None else None
    if extraction is None or not isinstance(operation, dict):
        raise ReviewTimelineEntryEditOperationConflict("同轮 operation receipt 身份已损坏")
    extraction_family = extraction.get("operation_type")
    family_keys = {"type", "operation_type"} & set(operation)
    expected_key = "operation_type" if extraction_family == "preference_update" else "type"
    if (
        extraction_family not in _TURN_OPERATION_FAMILIES
        or family_keys != {expected_key}
        or operation.get(expected_key) != extraction_family
    ):
        raise ReviewTimelineEntryEditOperationConflict("同轮 operation family 身份已损坏")
    if (
        _persisted_uuid(extraction.get("client_turn_id"), label="client_turn_id")
        != client_turn_id
        or _persisted_uuid(operation.get("client_turn_id"), label="client_turn_id")
        != client_turn_id
    ):
        raise ReviewTimelineEntryEditOperationConflict("同轮 operation turn 身份已损坏")
    return extraction_family


def _review_edit_operation_count(
    conn: Connection,
    user_id: str,
    client_turn_id: str,
) -> int:
    candidates = journal.read_operation_candidates_for_turn_in_transaction(
        conn,
        user_id,
        client_turn_id,
        maximum=MAX_TURN_OPERATION_CANDIDATES,
    )
    if len(candidates) > MAX_TURN_OPERATION_CANDIDATES:
        raise ReviewTimelineEntryEditOperationConflict("同轮 operation 候选超过安全上限")
    return sum(
        _classify_turn_candidate(candidate, client_turn_id)
        == "review_timeline_entry_edit"
        for candidate in candidates
    )


def _request_digest(client_turn_id: str, command: ReviewTimelineEntryEditCommand) -> str:
    return hashlib.sha256(_canonical_json({
        "client_turn_id": client_turn_id,
        "command": command.model_dump(mode="json", exclude_unset=True),
    }).encode("utf-8")).hexdigest()


def _proposal_digest(proposal) -> str:
    return hashlib.sha256(
        _canonical_json(proposal.model_dump(mode="json")).encode("utf-8"),
    ).hexdigest()


def _command(company: str | None, position: str | None, changes: dict):
    try:
        return ReviewTimelineEntryEditCommand.model_validate(
            {"company": company, "position": position, "changes": changes},
            strict=True,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("复盘历程修正参数不符合契约") from error


def _locate_target(
    conn: Connection,
    user_id: str,
    command: ReviewTimelineEntryEditCommand,
):
    conditions = ["source.user_id = ?", "source.kind = 'review'", "source.state = 'applied'"]
    params: list = [user_id]
    if command.company is not None:
        conditions.append(
            "(squash_whitespace(json_extract(CASE WHEN json_valid(source.extraction_json) "
            "THEN source.extraction_json ELSE '{}' END, '$.company')) = "
            "squash_whitespace(?) OR squash_whitespace(application.company) = "
            "squash_whitespace(?) OR NOT json_valid(source.extraction_json) OR "
            "trim(COALESCE(application.company, '')) = '')"
        )
        params.extend((command.company, command.company))
    if command.position is not None:
        conditions.append(
            "(squash_whitespace(json_extract(CASE WHEN json_valid(source.extraction_json) "
            "THEN source.extraction_json ELSE '{}' END, '$.position')) = "
            "squash_whitespace(?) OR squash_whitespace(application.position) = "
            "squash_whitespace(?) OR NOT json_valid(source.extraction_json) OR "
            "trim(COALESCE(application.position, '')) = '')"
        )
        params.extend((command.position, command.position))
    rows = conn.execute(
        "SELECT source.id, "
        "json_extract(CASE WHEN json_valid(source.extraction_json) "
        "THEN source.extraction_json ELSE '{}' END, '$.company'), "
        "json_extract(CASE WHEN json_valid(source.extraction_json) "
        "THEN source.extraction_json ELSE '{}' END, '$.position'), "
        "application.company, application.position FROM journal source "
        "LEFT JOIN timeline_entries entry "
        "ON entry.user_id = source.user_id AND entry.journal_id = source.id "
        "LEFT JOIN applications application ON application.user_id = entry.user_id "
        "AND application.id = entry.application_id WHERE "
        + " AND ".join(conditions)
        + " ORDER BY source.id DESC LIMIT ?",
        (*params, (MAX_SELECTOR_OPTIONS + 1) * 4),
    ).fetchall()
    if not rows:
        return {"status": "not_found"}

    bundles = []
    seen_sources: set[int] = set()
    seen_identities: set[tuple[str, str]] = set()
    for row in rows:
        if row[0] in seen_sources:
            continue
        seen_sources.add(row[0])
        raw_company = row[1] if isinstance(row[1], str) and row[1].strip() else row[3]
        raw_position = row[2] if isinstance(row[2], str) and row[2].strip() else row[4]
        raw_identity = (
            squash_whitespace(raw_company or ""),
            squash_whitespace(raw_position or ""),
        )
        if raw_identity in seen_identities:
            continue
        seen_identities.add(raw_identity)
        try:
            bundles.append(_load_bundle(conn, user_id, row[0]))
        except _EditTargetMissing as error:
            raise ReviewTimelineEntryEditOperationConflict(
                "最新匹配的复盘已缺少历程或岗位",
            ) from error
        except _UnsafeEditDependency as error:
            raise ReviewTimelineEntryEditOperationConflict(str(error)) from error
        if command.company is not None and command.position is not None:
            break
        if command.company is None and command.position is None:
            break
        if len(bundles) > MAX_SELECTOR_OPTIONS:
            raise ReviewTimelineEntryEditOperationConflict("复盘历程匹配超过安全上限")
    if not bundles:
        return {"status": "not_found"}
    if command.company is not None and command.position is None:
        positions = list(dict.fromkeys(bundle.target.position for bundle in bundles))
        if len(positions) > 1:
            return {"status": "ambiguous", "options": positions[:MAX_SELECTOR_OPTIONS]}
    if command.company is None and command.position is not None:
        identities = list(dict.fromkeys(
            f"{bundle.target.company}·{bundle.target.position}" for bundle in bundles
        ))
        if len(identities) > 1:
            return {"status": "ambiguous", "options": identities[:MAX_SELECTOR_OPTIONS]}
    return {"status": "ok", "bundle": bundles[0]}


def _review_extraction(raw: str):
    """Kept as a bounded feature-private parser used by architecture tests."""
    from ...ai_models import ReviewExtraction

    try:
        loaded = json.loads(raw)
        return ReviewExtraction.model_validate(loaded)
    except (json.JSONDecodeError, TypeError, ValidationError) as error:
        raise ReviewTimelineEntryEditOperationConflict("Review extraction 已损坏") from error
