"""Atomic long-term preference operations, trusted receipts, and turn recovery."""

from __future__ import annotations

import json
from collections import Counter
from sqlite3 import Connection
from uuid import UUID

from pydantic import ValidationError

from ...platform.database import now_iso, read_connection, transaction
from ..journal import public as journal
from . import repository
from .models import (
    MAX_ACTIVE_PREFERENCES,
    MAX_PERSISTED_PREFERENCE_JSON_CHARS,
    MAX_PREFERENCE_TOTAL_CHARS,
    PREFERENCE_UPDATE_CONTRACT_VERSION,
    PreferenceApplyChange,
    PreferenceApplyCommand,
    PreferenceOperationDTO,
    PreferenceOperationEffect,
    PreferenceOperationEffectDTO,
    PreferenceOperationEnvelope,
    PreferenceOperationResult,
    preference_structure_digest,
)

_OPERATION_COLUMNS = (
    "id, operation_id, state, created_time, processed_time, "
    "extraction_json, derivation_json, revision, content, kind"
)
_TURN_OPERATION_FAMILIES = frozenset({
    "application_update",
    "preference_update",
    "review_timeline_entry_edit",
    "review_record",
})
_MAX_TURN_OPERATION_CANDIDATES = 64
_MAX_TURN_OPERATION_JSON_CHARS = 500_000
class PreferenceOperationNotFound(LookupError):
    """Operation is absent or belongs to another user."""


class PreferenceOperationConflict(RuntimeError):
    """Operation identity reuse or corrupt projection/durable receipt."""


class PreferenceTurnAlreadyCommitted(PreferenceOperationConflict):
    """Another server operation ID already committed this assistant turn."""


def _canonical_uuid(value: str | UUID, *, field: str) -> str:
    try:
        canonical = str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field} 必须是 UUID") from error
    if isinstance(value, str) and value != canonical:
        raise ValueError(f"{field} 必须是规范 UUID")
    return canonical


def canonical_apply_command(changes: list[dict]) -> PreferenceApplyCommand:
    """Validate strictly, trim, and sort by key without leaking user values."""
    if not isinstance(changes, list):
        raise ValueError("偏好 apply 参数无效")
    command = None
    try:
        normalized = [PreferenceApplyChange.model_validate(item) for item in changes]
        normalized.sort(key=lambda item: item.key)
        command = PreferenceApplyCommand.model_validate({
            "changes": [item.model_dump() for item in normalized],
        })
    except (TypeError, ValueError, ValidationError):
        pass
    if command is None:
        # Raise outside except: even ``from None`` retains input_value in __context__,
        # which exception collectors could inspect.
        raise ValueError("偏好 apply 参数无效")
    return command


def _canonical_json(value: dict) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded) > MAX_PERSISTED_PREFERENCE_JSON_CHARS:
        raise PreferenceOperationConflict("偏好 operation receipt 超过安全上限")
    return encoded


def _safe_timestamp(value) -> str | None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 64
    ):
        return None
    return value


def _parse_envelope(raw: str | None, *, wrapped: bool) -> PreferenceOperationEnvelope:
    if not isinstance(raw, str) or len(raw) > MAX_PERSISTED_PREFERENCE_JSON_CHARS:
        raise PreferenceOperationConflict("偏好 operation receipt 缺失或超界")
    loaded = None
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    if loaded is None:
        raise PreferenceOperationConflict("偏好 operation receipt 不是有效 JSON")
    if wrapped:
        if not isinstance(loaded, dict) or set(loaded) != {"operation"}:
            raise PreferenceOperationConflict("偏好 operation derivation 形状无效")
        loaded = loaded["operation"]
    envelope = None
    try:
        envelope = PreferenceOperationEnvelope.model_validate(loaded)
    except (TypeError, ValueError, ValidationError):
        pass
    if envelope is None:
        raise PreferenceOperationConflict("偏好 operation receipt 无法校验")
    if envelope.model_dump() != loaded:
        raise PreferenceOperationConflict("偏好 operation receipt 不是 canonical contract")
    return envelope


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
        "AND operation_id = ?",
        (user_id, operation_id),
    ).fetchone()


def _classify_turn_candidate(
    *,
    operation_id,
    kind,
    extraction_json,
    derivation_json,
    client_turn_id: str,
) -> str:
    """Identify the trusted immediate family; block all writes on ambiguity."""
    if kind != "correction":
        raise PreferenceOperationConflict("该轮 operation kind 已损坏")
    canonical_operation = None
    try:
        canonical_operation = _canonical_uuid(operation_id, field="operation_id")
    except ValueError:
        pass
    if not isinstance(operation_id, str) or operation_id != canonical_operation:
        raise PreferenceOperationConflict("该轮 operation identity 已损坏")
    extraction = None
    derivation = None
    if (
        isinstance(extraction_json, str)
        and isinstance(derivation_json, str)
        and len(extraction_json) <= _MAX_TURN_OPERATION_JSON_CHARS
        and len(derivation_json) <= _MAX_TURN_OPERATION_JSON_CHARS
    ):
        try:
            extraction = json.loads(extraction_json)
            derivation = json.loads(derivation_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if (
        not isinstance(extraction, dict)
        or not isinstance(derivation, dict)
        or set(derivation) != {"operation"}
    ):
        raise PreferenceOperationConflict("该轮存在无法识别的 operation 回执")
    wrapped = derivation.get("operation")
    if not isinstance(wrapped, dict):
        raise PreferenceOperationConflict("该轮存在无法识别的 operation 回执")
    extraction_family = extraction.get("operation_type")
    derivation_family_keys = {
        name for name in ("type", "operation_type") if name in wrapped
    }
    expected_family_key = (
        "operation_type" if extraction_family == "preference_update" else "type"
    )
    derivation_family = (
        wrapped.get(next(iter(derivation_family_keys)))
        if len(derivation_family_keys) == 1
        else None
    )
    if (
        not isinstance(extraction_family, str)
        or derivation_family_keys != {expected_family_key}
        or not isinstance(derivation_family, str)
        or extraction_family != derivation_family
        or extraction_family not in _TURN_OPERATION_FAMILIES
    ):
        raise PreferenceOperationConflict("该轮 operation family 已损坏")
    extraction_turn = None
    derivation_turn = None
    try:
        extraction_turn = _canonical_uuid(
            extraction.get("client_turn_id"), field="client_turn_id",
        )
        derivation_turn = _canonical_uuid(
            wrapped.get("client_turn_id"), field="client_turn_id",
        )
    except ValueError:
        pass
    if (
        not isinstance(extraction.get("client_turn_id"), str)
        or not isinstance(wrapped.get("client_turn_id"), str)
        or extraction_turn != derivation_turn
        or extraction_turn != client_turn_id
    ):
        raise PreferenceOperationConflict("该轮 operation turn 已损坏")
    return extraction_family


def _rows_for_turn(conn: Connection, user_id: str, client_turn_id: str):
    candidates = journal.read_operation_candidates_for_turn_in_transaction(
        conn,
        user_id,
        client_turn_id,
        maximum=_MAX_TURN_OPERATION_CANDIDATES,
    )
    if len(candidates) > _MAX_TURN_OPERATION_CANDIDATES:
        raise PreferenceOperationConflict("该轮 operation 数量超过安全读取上限")
    rows = []
    for candidate in candidates:
        family = _classify_turn_candidate(
            operation_id=candidate.operation_id,
            kind=candidate.kind,
            extraction_json=candidate.extraction_json,
            derivation_json=candidate.derivation_json,
            client_turn_id=client_turn_id,
        )
        if family != "preference_update":
            continue
        row = _operation_row(conn, user_id, candidate.operation_id)
        if row is None or row[0] != candidate.journal_id:
            raise PreferenceOperationConflict("该轮偏好 operation 身份已损坏")
        rows.append(row)
    if len(rows) > 1:
        raise PreferenceOperationConflict("同一轮存在多条偏好 operation")
    return rows


def _effect_is_current(conn: Connection, user_id: str,
                       effect: PreferenceOperationEffect) -> bool:
    row = conn.execute(
        "SELECT id, revision FROM preferences WHERE user_id = ? AND key = ?",
        (user_id, effect.key),
    ).fetchone()
    if effect.outcome in {"deleted", "missing"}:
        return row is None
    return row == (effect.final_id, effect.final_revision)


def _envelope_from_row(row) -> PreferenceOperationEnvelope:
    proposal = _parse_envelope(row[5], wrapped=False)
    receipt = _parse_envelope(row[6], wrapped=True)
    if proposal != receipt:
        raise PreferenceOperationConflict("偏好 operation 双份 receipt 不一致")
    return proposal


def _command_skeleton(command: PreferenceApplyCommand) -> list[tuple[str, str]]:
    return [(item.op, item.key) for item in command.changes]


def _require_same_skeleton(row, command: PreferenceApplyCommand) -> None:
    persisted = _envelope_from_row(row)
    if [(item.action, item.key) for item in persisted.actions] != _command_skeleton(command):
        raise PreferenceOperationConflict("本轮偏好 operation 已绑定另一组 action/key")


def _dto(conn: Connection, user_id: str, row) -> dict:
    (
        _journal_id,
        operation_id,
        state,
        created_time,
        processed_time,
        extraction_json,
        derivation_json,
        revision,
        content,
        kind,
    ) = row
    safe_created = _safe_timestamp(created_time)
    if (
        safe_created is None
        or processed_time != safe_created
        or kind != "correction"
        or state != "applied"
        or revision != 0
    ):
        raise PreferenceOperationConflict("偏好 operation lifecycle 已损坏")
    try:
        canonical_operation = _canonical_uuid(operation_id, field="operation_id")
    except ValueError as error:
        raise PreferenceOperationConflict("偏好 operation identity 已损坏") from error
    proposal = _parse_envelope(extraction_json, wrapped=False)
    receipt = _parse_envelope(derivation_json, wrapped=True)
    if proposal != receipt:
        raise PreferenceOperationConflict("偏好 operation 双份 receipt 不一致")
    if proposal.operation_id != canonical_operation:
        raise PreferenceOperationConflict("偏好 operation column/receipt identity 不一致")
    expected_content = f"[已更新长期偏好：{proposal.result.changed_count} 项变化]"
    if content != expected_content:
        raise PreferenceOperationConflict("偏好 operation 审计摘要已损坏")
    effect_dtos = [
        PreferenceOperationEffectDTO(
            **effect.model_dump(),
            current=_effect_is_current(conn, user_id, effect),
        )
        for effect in proposal.effects
    ]
    dto = None
    try:
        dto = PreferenceOperationDTO(
            operation_type="preference_update",
            contract_version=PREFERENCE_UPDATE_CONTRACT_VERSION,
            state="completed",
            operation_id=canonical_operation,
            client_turn_id=proposal.client_turn_id,
            created_time=safe_created,
            effects=effect_dtos,
            current=all(item.current for item in effect_dtos),
            result=proposal.result,
        ).model_dump()
    except (TypeError, ValueError, ValidationError):
        pass
    if dto is None:
        raise PreferenceOperationConflict("偏好 operation DTO 无法校验")
    return dto


def _result(effects: list[PreferenceOperationEffect]) -> dict:
    counts = Counter(item.outcome for item in effects)
    changed = counts["created"] + counts["updated"] + counts["deleted"]
    return {
        "requested_count": len(effects),
        "changed_count": changed,
        "unchanged_count": counts["unchanged"],
        "created_count": counts["created"],
        "updated_count": counts["updated"],
        "deleted_count": counts["deleted"],
        "missing_count": counts["missing"],
    }


def _planned_projection(snapshot: list[dict], command: PreferenceApplyCommand) -> dict[str, str]:
    projected = {item["key"]: item["value"] for item in snapshot}
    for change in command.changes:
        if change.op == "set":
            projected[change.key] = change.value
        else:
            projected.pop(change.key, None)
    if len(projected) > MAX_ACTIVE_PREFERENCES:
        raise ValueError(f"长期偏好最多保存 {MAX_ACTIVE_PREFERENCES} 项")
    total_chars = sum(len(key) + len(value) for key, value in projected.items())
    if total_chars > MAX_PREFERENCE_TOTAL_CHARS:
        raise ValueError(
            f"长期偏好的 key 与 value 合计最多 {MAX_PREFERENCE_TOTAL_CHARS} 个字符",
        )
    return projected


def execute_preference_update_operation(
    db_path: str,
    user_id: str,
    *,
    operation_id: str | UUID,
    client_turn_id: str | UUID,
    changes: list[dict],
) -> dict:
    """Apply a preference batch and terminal receipt in one BEGIN IMMEDIATE."""
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    canonical_operation = _canonical_uuid(operation_id, field="operation_id")
    canonical_turn = _canonical_uuid(client_turn_id, field="client_turn_id")
    command = canonical_apply_command(changes)

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_turn = _rows_for_turn(conn, user_id, canonical_turn)
        if len(existing_turn) > 1:
            raise PreferenceOperationConflict("同一轮存在多笔偏好 operation")
        if existing_turn:
            # Validate the durable row first; a tampered operation ID must not masquerade
            # as a normal first-commit-wins conflict.
            existing_dto = _dto(conn, user_id, existing_turn[0])
            if existing_dto["operation_id"] != canonical_operation:
                # Receipts omit value/hash, so only the original ID is idempotent; reject
                # every new ID under first-commit-wins semantics.
                raise PreferenceTurnAlreadyCommitted("本轮首笔偏好 operation 已提交")
            _require_same_skeleton(existing_turn[0], command)
            return existing_dto

        existing_operation = _operation_row(conn, user_id, canonical_operation)
        if existing_operation is not None:
            dto = _dto(conn, user_id, existing_operation)
            if dto["client_turn_id"] != canonical_turn:
                raise PreferenceOperationConflict("operation_id 已用于另一轮偏好更新")
            _require_same_skeleton(existing_operation, command)
            return dto
        if conn.execute(
            "SELECT 1 FROM journal WHERE operation_id = ? LIMIT 1",
            (canonical_operation,),
        ).fetchone() is not None:
            raise PreferenceOperationNotFound("偏好 operation 不存在")

        before_snapshot = repository._snapshot(conn, user_id)
        projected = _planned_projection(before_snapshot, command)
        current_by_key = {item["key"]: item for item in before_snapshot}
        timestamp = now_iso()
        changes_before = conn.total_changes
        effects: list[PreferenceOperationEffect] = []

        for change in command.changes:
            before = current_by_key.get(change.key)
            if change.op == "set":
                if before is None:
                    final = repository._insert(
                        conn, user_id, change.key, change.value, timestamp,
                    )
                    current_by_key[change.key] = final
                    effect = PreferenceOperationEffect(
                        action="set",
                        key=change.key,
                        outcome="created",
                        final_id=final["id"],
                        final_revision=final["revision"],
                    )
                elif before["value"] == change.value:
                    effect = PreferenceOperationEffect(
                        action="set",
                        key=change.key,
                        outcome="unchanged",
                        before_id=before["id"],
                        before_revision=before["revision"],
                        final_id=before["id"],
                        final_revision=before["revision"],
                    )
                else:
                    final = repository._update(
                        conn, user_id, before, change.value, timestamp,
                    )
                    current_by_key[change.key] = final
                    effect = PreferenceOperationEffect(
                        action="set",
                        key=change.key,
                        outcome="updated",
                        before_id=before["id"],
                        before_revision=before["revision"],
                        final_id=final["id"],
                        final_revision=final["revision"],
                    )
            elif before is None:
                effect = PreferenceOperationEffect(
                    action="delete",
                    key=change.key,
                    outcome="missing",
                )
            else:
                repository._delete(conn, user_id, before)
                current_by_key.pop(change.key)
                effect = PreferenceOperationEffect(
                    action="delete",
                    key=change.key,
                    outcome="deleted",
                    before_id=before["id"],
                    before_revision=before["revision"],
                )
            effects.append(effect)

        result_payload = _result(effects)
        if result_payload["changed_count"] == 0:
            if conn.total_changes != changes_before:
                raise PreferenceOperationConflict("no-op 偏好命令发生了写入")
            return {
                "status": "no_change",
                "effects": [item.model_dump() for item in effects],
                "result": result_payload,
            }

        after_snapshot = repository._snapshot(conn, user_id)
        after_values = {item["key"]: item["value"] for item in after_snapshot}
        if after_values != projected:
            raise PreferenceOperationConflict("偏好 projection 与计划结果不一致")
        # A database trigger also creates a stable valueless owner in this transaction.
        expected_projection_writes = (
            result_payload["changed_count"] + result_payload["created_count"]
        )
        if conn.total_changes - changes_before != expected_projection_writes:
            raise PreferenceOperationConflict("偏好批量写入发生了未展示的额外变化")

        envelope_payload = {
            "operation_type": "preference_update",
            "contract_version": PREFERENCE_UPDATE_CONTRACT_VERSION,
            "operation_id": canonical_operation,
            "client_turn_id": canonical_turn,
            "actions": [
                {"action": item.op, "key": item.key}
                for item in command.changes
            ],
            "effects": [item.model_dump() for item in effects],
            "result": result_payload,
        }
        envelope_payload["structure_digest"] = preference_structure_digest(envelope_payload)
        envelope = None
        result = None
        try:
            envelope = PreferenceOperationEnvelope.model_validate(envelope_payload)
            result = PreferenceOperationResult.model_validate(result_payload)
        except (TypeError, ValueError, ValidationError):  # pragma: no cover
            pass
        if envelope is None or result is None:  # pragma: no cover
            raise PreferenceOperationConflict("偏好 operation receipt 构造失败")
        extraction_json = _canonical_json(envelope.model_dump())
        derivation_json = _canonical_json({"operation": envelope.model_dump()})
        content = f"[已更新长期偏好：{result.changed_count} 项变化]"
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, processed_time, "
            "extraction_json, derivation_json, state, operation_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, ?, 'applied', ?)",
            (
                user_id,
                content,
                timestamp,
                timestamp,
                extraction_json,
                derivation_json,
                canonical_operation,
            ),
        )
        if conn.total_changes - changes_before != expected_projection_writes + 1:
            raise PreferenceOperationConflict("偏好 operation 审计落底发生了额外变化")
        terminal = _operation_row(conn, user_id, canonical_operation)
        if terminal is None:  # pragma: no cover
            raise RuntimeError("preference operation insert lost")
        dto = _dto(conn, user_id, terminal)
        if not dto["current"]:
            raise PreferenceOperationConflict("偏好 operation 新回执不是 current")
        return dto


def get_preference_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict | None:
    canonical = _canonical_uuid(operation_id, field="operation_id")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _operation_row(conn, user_id, canonical)
        return _dto(conn, user_id, row) if row is not None else None


def list_preference_operations_for_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str | UUID,
) -> list[dict]:
    canonical = _canonical_uuid(client_turn_id, field="client_turn_id")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        rows = _rows_for_turn(conn, user_id, canonical)
        if len(rows) > 1:
            raise PreferenceOperationConflict("同一轮存在多笔偏好 operation")
        operations = [_dto(conn, user_id, row) for row in rows]
        if any(item["client_turn_id"] != canonical for item in operations):
            raise PreferenceOperationConflict("偏好 operation turn identity 已损坏")
        return operations
