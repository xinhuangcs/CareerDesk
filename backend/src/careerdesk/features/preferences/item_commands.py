"""Terminal item preference commands, CAS, and changed-only receipts."""

from __future__ import annotations

import json
from sqlite3 import Connection
from uuid import UUID, uuid4

from pydantic import ValidationError

from ...platform.database import now_iso, read_connection, transaction
from . import repository
from .item_command_models import (
    PREFERENCE_ITEM_COMMAND_CONTRACT_VERSION,
    PREFERENCE_ITEM_OPERATION_CONTRACT_VERSION,
    PreferenceItemCommandCancelInput,
    PreferenceItemCommandInput,
    PreferenceItemCommandResult,
    PreferenceItemCommandStatus,
    PreferenceItemOperationDTO,
    PreferenceItemOperationEnvelope,
    preference_item_structure_digest,
)
from .models import (
    MAX_JSON_SAFE_INTEGER,
    MAX_PERSISTED_PREFERENCE_JSON_CHARS,
    MAX_PREFERENCE_TOTAL_CHARS,
)

_COMMAND_COLUMNS = (
    "command_id, action, target_id, expected_revision, state, outcome, "
    "journal_id, operation_id, error_code, finished_time"
)
_OPERATION_COLUMNS = (
    "id, user_id, kind, content, created_time, processed_time, extraction_json, "
    "derivation_json, state, revision, parent_journal_id, operation_id"
)
_ERROR_MESSAGES = {
    "target_missing": "该偏好已不存在，请刷新后重试。",
    "target_changed": "该偏好已在别处更新，请刷新后重试。",
    "limit_exceeded": "更新后将超过长期偏好的总长度上限。",
    "projection_invalid": "当前偏好状态无法安全校验，本次没有修改。",
}


class PreferenceItemCommandConflict(RuntimeError):
    """Command identity reuse or corrupt owner, ledger, or receipt."""


def _canonical_uuid(value: str | UUID, *, field: str) -> str:
    try:
        canonical = str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field} 必须是 UUID") from error
    if isinstance(value, str) and value != canonical:
        raise ValueError(f"{field} 必须是规范 UUID")
    return canonical


def canonical_item_command(payload: object) -> PreferenceItemCommandInput:
    command = None
    try:
        command = PreferenceItemCommandInput.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        pass
    if command is None:
    # Raise after except so Pydantic input_value is not retained through __context__.
        raise ValueError("偏好逐项命令参数无效")
    return command


def canonical_cancel_command(payload: object) -> PreferenceItemCommandCancelInput:
    command = None
    try:
        command = PreferenceItemCommandCancelInput.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        pass
    if command is None:
        raise ValueError("偏好逐项取消参数无效")
    return command


def _canonical_json(value: dict) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded) > MAX_PERSISTED_PREFERENCE_JSON_CHARS:
        raise PreferenceItemCommandConflict("偏好逐项 operation receipt 超界")
    return encoded


def _command_row(conn: Connection, user_id: str, command_id: str):
    return conn.execute(
        f"SELECT {_COMMAND_COLUMNS} FROM preference_item_commands "
        "WHERE user_id = ? AND command_id = ?",
        (user_id, command_id),
    ).fetchone()


def _command_owner_row(conn: Connection, user_id: str, command_id: str):
    return conn.execute(
        "SELECT user_id, command_id, created_time "
        "FROM preference_item_command_owners "
        "WHERE user_id = ? AND command_id = ?",
        (user_id, command_id),
    ).fetchone()


def _owned_command_row(conn: Connection, user_id: str, command_id: str):
    owner = _command_owner_row(conn, user_id, command_id)
    command = _command_row(conn, user_id, command_id)
    if (owner is None) != (command is None):
        raise PreferenceItemCommandConflict("偏好逐项 command owner/terminal 已损坏")
    if owner is not None and owner != (user_id, command_id, command[9]):
        raise PreferenceItemCommandConflict("偏好逐项 command owner identity 已损坏")
    return command


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal "
        "WHERE user_id = ? AND operation_id = ?",
        (user_id, operation_id),
    ).fetchone()


def _operation_row_by_journal_id(conn: Connection, journal_id: int):
    return conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE id = ?",
        (journal_id,),
    ).fetchone()


def _parse_envelope(raw: str | None, *, wrapped: bool) -> PreferenceItemOperationEnvelope:
    loaded = None
    if isinstance(raw, str) and len(raw) <= MAX_PERSISTED_PREFERENCE_JSON_CHARS:
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if wrapped:
        if not isinstance(loaded, dict) or set(loaded) != {"operation"}:
            loaded = None
        else:
            loaded = loaded.get("operation")
    envelope = None
    if loaded is not None:
        try:
            envelope = PreferenceItemOperationEnvelope.model_validate(loaded)
        except (TypeError, ValueError, ValidationError):
            pass
    if envelope is None or envelope.model_dump() != loaded:
        raise PreferenceItemCommandConflict("偏好逐项 operation receipt 已损坏")
    return envelope


def _operation_dto(conn: Connection, user_id: str, row) -> dict:
    (
        journal_id,
        journal_user,
        kind,
        content,
        created_time,
        processed_time,
        extraction_json,
        derivation_json,
        state,
        revision,
        parent_journal_id,
        operation_id,
    ) = tuple(row)
    proposal = _parse_envelope(extraction_json, wrapped=False)
    receipt = _parse_envelope(derivation_json, wrapped=True)
    if proposal != receipt:
        raise PreferenceItemCommandConflict("偏好逐项 operation 双份 receipt 不一致")
    if (
        journal_user != user_id
        or kind != "correction"
        or content != "[已在设置中更新长期偏好：1 项变化]"
        or created_time != processed_time
        or state != "applied"
        or revision != 0
        or parent_journal_id is not None
        or operation_id != proposal.operation_id
    ):
        raise PreferenceItemCommandConflict("偏好逐项 operation lifecycle 已损坏")
    command = _owned_command_row(conn, user_id, proposal.command_id)
    if command is None:
        raise PreferenceItemCommandConflict("偏好逐项 operation 缺少 command owner")
    if (
        command[4] != "completed"
        or command[5] != proposal.result.outcome
        or command[6] != journal_id
        or command[7] != proposal.operation_id
        or command[0] != proposal.command_id
        or command[1] != proposal.action
        or (command[2], command[3]) != (
            proposal.result.before.id,
            proposal.result.before.revision,
        )
        or command[8] is not None
        or command[9] != created_time
    ):
        raise PreferenceItemCommandConflict("偏好逐项 command/operation identity 不一致")
    snapshot = repository._snapshot(conn, user_id)
    if proposal.result.outcome == "updated":
        current = any(
            item["id"] == proposal.result.final.id
            and item["revision"] == proposal.result.final.revision
            for item in snapshot
        )
    else:
        current = not any(item["key"] == proposal.key for item in snapshot)
    dto = None
    try:
        dto = PreferenceItemOperationDTO(
            operation_type="preference_item_change",
            contract_version=PREFERENCE_ITEM_OPERATION_CONTRACT_VERSION,
            state="completed",
            operation_id=proposal.operation_id,
            command_id=proposal.command_id,
            action=proposal.action,
            key=proposal.key,
            result=proposal.result,
            created_time=created_time,
            current=current,
        ).model_dump()
    except (TypeError, ValueError, ValidationError):
        pass
    if dto is None:
        raise PreferenceItemCommandConflict("偏好逐项 operation DTO 已损坏")
    return dto


def _status(conn: Connection, user_id: str, row) -> dict:
    (
        command_id,
        action,
        target_id,
        expected_revision,
        state,
        outcome,
        journal_id,
        operation_id,
        error_code,
        finished_time,
    ) = tuple(row)
    owner = _command_owner_row(conn, user_id, command_id)
    if owner != (user_id, command_id, finished_time):
        raise PreferenceItemCommandConflict("偏好逐项 command owner identity 已损坏")
    result = None
    error = None
    if state == "completed":
        if error_code is not None:
            raise PreferenceItemCommandConflict("completed command error 已损坏")
        before = {"id": target_id, "revision": expected_revision}
        if outcome == "updated":
            final = {"id": target_id, "revision": expected_revision + 1}
        elif outcome == "deleted":
            final = None
        elif outcome == "no_change":
            final = before
        else:
            raise PreferenceItemCommandConflict("偏好逐项 command outcome 已损坏")
        result = {"outcome": outcome, "before": before, "final": final}
    elif state == "rejected":
        if outcome is not None or journal_id is not None or operation_id is not None:
            raise PreferenceItemCommandConflict("rejected command identity 已损坏")
        message = _ERROR_MESSAGES.get(error_code)
        if message is None:
            raise PreferenceItemCommandConflict("偏好逐项 command error 已损坏")
        error = {"code": error_code, "message": message}
    elif state != "cancelled":
        raise PreferenceItemCommandConflict("偏好逐项 command state 已损坏")
    elif any(item is not None for item in (
        outcome, journal_id, operation_id, error_code,
    )):
        raise PreferenceItemCommandConflict("cancelled command identity 已损坏")

    status = None
    try:
        status = PreferenceItemCommandStatus(
            contract_version=PREFERENCE_ITEM_COMMAND_CONTRACT_VERSION,
            command_id=command_id,
            state=state,
            action=action,
            target={"id": target_id, "revision": expected_revision},
            result=result,
            error=error,
            operation_id=operation_id,
            finished_time=finished_time,
        ).model_dump()
    except (TypeError, ValueError, ValidationError):
        pass
    if status is None:
        raise PreferenceItemCommandConflict("偏好逐项 command receipt 已损坏")

    if state == "completed" and outcome in {"updated", "deleted"}:
        operation_row = _operation_row_by_journal_id(conn, journal_id)
        if operation_row is None or operation_row[0] != journal_id:
            raise PreferenceItemCommandConflict("偏好逐项 command 缺少 operation receipt")
        operation = _operation_dto(conn, user_id, operation_row)
        if (
            operation["command_id"] != command_id
            or operation["action"] != action
            or operation["result"] != result
        ):
            raise PreferenceItemCommandConflict("偏好逐项 command/operation 回执不一致")
    elif any(item is not None for item in (journal_id, operation_id)):
        raise PreferenceItemCommandConflict("无变化命令不应关联 operation")
    return status


def _require_same_skeleton(row, command) -> None:
    if (
        row[1] != command.action
        or row[2] != command.target.id
        or row[3] != command.target.revision
    ):
        raise PreferenceItemCommandConflict("command_id 已绑定另一条偏好命令")


def _insert_command(
    conn: Connection,
    user_id: str,
    command_id: str,
    command,
    *,
    state: str,
    timestamp: str,
    outcome: str | None = None,
    journal_id: int | None = None,
    operation_id: str | None = None,
    error_code: str | None = None,
) -> dict:
    before = conn.total_changes
    conn.execute(
        "INSERT INTO preference_item_command_owners "
        "(user_id, command_id, created_time) VALUES (?, ?, ?)",
        (user_id, command_id, timestamp),
    )
    conn.execute(
        "INSERT INTO preference_item_commands "
        "(user_id, command_id, action, target_id, expected_revision, state, "
        "outcome, journal_id, operation_id, error_code, finished_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            command_id,
            command.action,
            command.target.id,
            command.target.revision,
            state,
            outcome,
            journal_id,
            operation_id,
            error_code,
            timestamp,
        ),
    )
    if conn.total_changes - before != 2:
        raise PreferenceItemCommandConflict("偏好逐项 command 落底发生额外写入")
    row = _owned_command_row(conn, user_id, command_id)
    if row is None:
        raise RuntimeError("preference item command insert lost")
    return _status(conn, user_id, row)


def _persist_rejection(
    conn: Connection,
    user_id: str,
    command_id: str,
    command: PreferenceItemCommandInput,
    code: str,
) -> dict:
    return _insert_command(
        conn,
        user_id,
        command_id,
        command,
        state="rejected",
        timestamp=now_iso(),
        error_code=code,
    )


def execute_preference_item_command(
    db_path: str,
    user_id: str,
    command_id: str | UUID,
    command: PreferenceItemCommandInput,
) -> tuple[dict, bool]:
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    canonical_command_id = _canonical_uuid(command_id, field="command_id")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _owned_command_row(conn, user_id, canonical_command_id)
        if existing is not None:
            _require_same_skeleton(existing, command)
            return _status(conn, user_id, existing), False

        try:
            snapshot = repository._snapshot(conn, user_id)
        except repository.PreferenceProjectionConflict:
            return _persist_rejection(
                conn, user_id, canonical_command_id, command, "projection_invalid",
            ), True
        before = next(
            (item for item in snapshot if item["id"] == command.target.id),
            None,
        )
        if before is None:
            return _persist_rejection(
                conn, user_id, canonical_command_id, command, "target_missing",
            ), True
        if before["revision"] != command.target.revision:
            return _persist_rejection(
                conn, user_id, canonical_command_id, command, "target_changed",
            ), True

        timestamp = now_iso()
        if command.action == "set" and before["value"] == command.value:
            return _insert_command(
                conn,
                user_id,
                canonical_command_id,
                command,
                state="completed",
                outcome="no_change",
                timestamp=timestamp,
            ), True
        if command.action == "set":
            if before["revision"] >= MAX_JSON_SAFE_INTEGER:
                return _persist_rejection(
                    conn,
                    user_id,
                    canonical_command_id,
                    command,
                    "projection_invalid",
                ), True
            total_chars = sum(
                len(item["key"]) + len(
                    command.value if item["id"] == before["id"] else item["value"]
                )
                for item in snapshot
            )
            if total_chars > MAX_PREFERENCE_TOTAL_CHARS:
                return _persist_rejection(
                    conn, user_id, canonical_command_id, command, "limit_exceeded",
                ), True
            final = repository._update(
                conn, user_id, before, command.value, timestamp,
            )
            outcome = "updated"
            final_target = {"id": final["id"], "revision": final["revision"]}
        else:
            repository._delete(conn, user_id, before)
            outcome = "deleted"
            final_target = None

        result = PreferenceItemCommandResult(
            outcome=outcome,
            before={"id": before["id"], "revision": before["revision"]},
            final=final_target,
        )
        operation_id = str(uuid4())
        envelope_payload = {
            "operation_type": "preference_item_change",
            "contract_version": PREFERENCE_ITEM_OPERATION_CONTRACT_VERSION,
            "operation_id": operation_id,
            "command_id": canonical_command_id,
            "action": command.action,
            "key": before["key"],
            "result": result.model_dump(),
        }
        envelope_payload["structure_digest"] = preference_item_structure_digest(
            envelope_payload,
        )
        envelope = PreferenceItemOperationEnvelope.model_validate(envelope_payload)
        encoded = _canonical_json(envelope.model_dump())
        wrapped = _canonical_json({"operation": envelope.model_dump()})
        before_journal = conn.total_changes
        cursor = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, processed_time, "
            "extraction_json, derivation_json, state, operation_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, ?, 'applied', ?)",
            (
                user_id,
                "[已在设置中更新长期偏好：1 项变化]",
                timestamp,
                timestamp,
                encoded,
                wrapped,
                operation_id,
            ),
        )
        if (
            conn.total_changes - before_journal != 1
            or cursor.lastrowid is None
        ):
            raise PreferenceItemCommandConflict("偏好逐项 operation 落底发生额外写入")
        status = _insert_command(
            conn,
            user_id,
            canonical_command_id,
            command,
            state="completed",
            outcome=outcome,
            journal_id=cursor.lastrowid,
            operation_id=operation_id,
            timestamp=timestamp,
        )
        after = repository._snapshot(conn, user_id)
        if outcome == "updated":
            current = next((item for item in after if item["id"] == before["id"]), None)
            if current is None or (
                current["revision"] != before["revision"] + 1
                or current["value"] != command.value
            ):
                raise PreferenceItemCommandConflict("偏好逐项更新后投影漂移")
        elif any(item["id"] == before["id"] for item in after):
            raise PreferenceItemCommandConflict("偏好逐项删除后投影漂移")
        return status, True


def cancel_preference_item_command_if_absent(
    db_path: str,
    user_id: str,
    command_id: str | UUID,
    command: PreferenceItemCommandCancelInput,
) -> tuple[dict, bool]:
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    canonical_command_id = _canonical_uuid(command_id, field="command_id")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _owned_command_row(conn, user_id, canonical_command_id)
        if existing is not None:
            _require_same_skeleton(existing, command)
            return _status(conn, user_id, existing), False
        return _insert_command(
            conn,
            user_id,
            canonical_command_id,
            command,
            state="cancelled",
            timestamp=now_iso(),
        ), True


def get_preference_item_command_status(
    db_path: str,
    user_id: str,
    command_id: str | UUID,
) -> dict | None:
    canonical = _canonical_uuid(command_id, field="command_id")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _owned_command_row(conn, user_id, canonical)
        return _status(conn, user_id, row) if row is not None else None


def get_preference_item_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict | None:
    canonical = _canonical_uuid(operation_id, field="operation_id")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        owner = conn.execute(
            "SELECT journal_id FROM preference_item_commands "
            "WHERE user_id = ? AND operation_id = ?",
            (user_id, canonical),
        ).fetchone()
        if owner is not None:
            row = _operation_row_by_journal_id(conn, owner[0])
            if row is None:
                raise PreferenceItemCommandConflict(
                    "偏好逐项 operation owner 指向缺失 journal",
                )
            return _operation_dto(conn, user_id, row)
        # Same-tenant occupied journal identity is not a 404; cross-tenant remains hidden.
        if _operation_row(conn, user_id, canonical) is not None:
            raise PreferenceItemCommandConflict("operation_id 不属于偏好逐项命令")
        return None
