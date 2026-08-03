"""Read models, startup recovery and the undo-preview bridge."""

from __future__ import annotations

from sqlite3 import Connection
from uuid import UUID

from .....platform.database import now_iso, read_connection
from ..record_models import (
    MAX_PENDING_REVIEW_RECORD_CLARIFICATIONS,
    MAX_PENDING_REVIEW_RECORD_CONFIRMATIONS,
    ReviewRecordOperationError,
)
from .dto import _dto, _operation_rows_for_turn, _terminal_operation
from .errors import ReviewRecordOperationConflict, ReviewRecordOperationNotFound
from .finalize import _terminal_derivation
from .rows import (
    _OPERATION_COLUMNS,
    _canonical_json,
    _canonical_uuid,
    _operation_row,
    _proposal,
    _validate_user_id,
)


def get_review_record_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict | None:
    _validate_user_id(user_id)
    canonical = _canonical_uuid(operation_id, label="operation_id")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _operation_row(conn, user_id, canonical)
        return _dto(conn, user_id, row) if row is not None else None


def list_review_record_operations_for_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str | UUID,
) -> list[dict]:
    _validate_user_id(user_id)
    canonical = _canonical_uuid(client_turn_id, label="client_turn_id")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        rows = _operation_rows_for_turn(conn, user_id, canonical)
        operations = [_dto(conn, user_id, row) for row in rows]
    if any(operation["client_turn_id"] != canonical for operation in operations):
        raise ReviewRecordOperationConflict("record_review turn 回执身份已损坏")
    return operations


def list_pending_review_record_confirmations(db_path: str, user_id: str) -> list[dict]:
    """Return the newest valid confirmation preview for every Review target."""
    _validate_user_id(user_id)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND operation_id IS NOT NULL "
            "AND state = 'awaiting_user' "
            "AND (json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json "
            "ELSE '{}' END, '$.operation_type') = 'review_record' OR "
            "json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'review_record') "
            "ORDER BY id DESC LIMIT ?",
            (user_id, MAX_PENDING_REVIEW_RECORD_CONFIRMATIONS + 1),
        ).fetchall()
        if len(rows) > MAX_PENDING_REVIEW_RECORD_CONFIRMATIONS:
            raise ReviewRecordOperationConflict("待确认复盘超过安全读取上限")
        pending: list[dict] = []
        seen_targets: set[int] = set()
        for row in rows:
            operation = _dto(conn, user_id, row)
            target_id = operation["target_journal_id"]
            if target_id in seen_targets:
                raise ReviewRecordOperationConflict("同一复盘存在多条待确认方案")
            if operation["state"] != "pending_confirmation":
                raise ReviewRecordOperationConflict("待确认复盘列表与规范状态不一致")
            pending.append(operation)
            seen_targets.add(target_id)
        return pending


def list_pending_review_record_clarifications(db_path: str, user_id: str) -> list[dict]:
    """Return the newest completed clarification per still-awaiting Review target."""
    _validate_user_id(user_id)
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
            "AND kind = 'correction' AND operation_id IS NOT NULL AND state = 'applied' "
            "AND (json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json "
            "ELSE '{}' END, '$.operation_type') = 'review_record' OR "
            "json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'review_record') "
            "AND parent_journal_id IN (SELECT target.id FROM journal target "
            "WHERE target.user_id = ? AND target.kind = 'review' "
            "AND target.state = 'awaiting_user') "
            "AND NOT EXISTS (SELECT 1 FROM journal processing "
            "WHERE processing.user_id = ? AND processing.kind = 'correction' "
            "AND processing.parent_journal_id = journal.parent_journal_id "
            "AND processing.operation_id IS NOT NULL "
            "AND processing.state IN ('pending', 'awaiting_user') "
            "AND (json_extract(CASE WHEN json_valid(processing.extraction_json) "
            "THEN processing.extraction_json ELSE '{}' END, '$.operation_type') "
            "= 'review_record' OR json_extract(CASE WHEN "
            "json_valid(processing.derivation_json) THEN processing.derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'review_record')) "
            "AND id IN (SELECT MAX(candidate.id) FROM journal candidate "
            "WHERE candidate.user_id = ? AND candidate.kind = 'correction' "
            "AND candidate.operation_id IS NOT NULL AND candidate.state = 'applied' "
            "AND (json_extract(CASE WHEN json_valid(candidate.extraction_json) "
            "THEN candidate.extraction_json ELSE '{}' END, '$.operation_type') "
            "= 'review_record' OR json_extract(CASE WHEN "
            "json_valid(candidate.derivation_json) THEN candidate.derivation_json "
            "ELSE '{}' END, '$.operation.type') = 'review_record') "
            "GROUP BY candidate.parent_journal_id) "
            "ORDER BY id DESC LIMIT ?",
            (
                user_id,
                user_id,
                user_id,
                user_id,
                MAX_PENDING_REVIEW_RECORD_CLARIFICATIONS + 1,
            ),
        ).fetchall()
        if len(rows) > MAX_PENDING_REVIEW_RECORD_CLARIFICATIONS:
            raise ReviewRecordOperationConflict("待补充复盘超过安全读取上限")
        pending: list[dict] = []
        seen_targets: set[int] = set()
        for row in rows:
            operation = _dto(conn, user_id, row)
            target_id = operation["target_journal_id"]
            if target_id in seen_targets:
                continue
            if operation["outcome"] != "needs_clarification":
                raise ReviewRecordOperationConflict("待补充复盘回执与目标状态不一致")
            if operation["target_current_state"] == "awaiting_user":
                pending.append(operation)
                seen_targets.add(target_id)
        return pending


def recover_interrupted_review_record_operations_in_transaction(
    conn: Connection,
) -> dict[str, int]:
    """Startup-only recovery; caller owns the single-instance transaction/lock."""
    rows = conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE kind = 'correction' "
        "AND operation_id IS NOT NULL AND state = 'pending' "
        "AND (json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
        "ELSE '{}' END, '$.operation.type') = 'review_record' OR "
        "json_extract(CASE WHEN json_valid(extraction_json) THEN extraction_json "
        "ELSE '{}' END, '$.operation_type') = 'review_record') ORDER BY id",
    ).fetchall()
    operations_recovered = 0
    for row in rows:
        try:
            proposal = _proposal(row[5])
            error = ReviewRecordOperationError(
                code="interrupted",
                message="上次复盘提取因应用退出而中断；原文已保留，业务投影没有发布。",
            )
            finished = now_iso()
            derivation = _terminal_derivation(
                proposal,
                action="failed",
                finished_time=finished,
                error=error,
            )
        except ReviewRecordOperationConflict:
            # The row must still leave processing even when its untrusted proposal is corrupt.
            finished = now_iso()
            operation = {
                "type": "review_record",
                "action": "failed",
                "finished_time": finished,
                "error": {
                    "code": "contract_invalid",
                    "message": "中断的复盘操作命令已损坏，已安全终结。",
                },
            }
            # Keep the independently persisted turn identity when its canonical
            # processing receipt is still readable. This lets a same-turn retry
            # find the damaged operation and fail closed instead of executing it
            # a second time.
            try:
                processing = _terminal_operation(row[6])
                operation["client_turn_id"] = _canonical_uuid(
                    processing.get("client_turn_id"),
                    label="client_turn_id",
                )
            except (ReviewRecordOperationConflict, ValueError):
                pass
            derivation = {"operation": operation}
        operations_recovered += conn.execute(
            "UPDATE journal SET processed_time = ?, derivation_json = ?, state = 'failed', "
            "revision = revision + 1 WHERE id = ? AND kind = 'correction' "
            "AND state = 'pending' AND revision = 0",
            (finished, _canonical_json(derivation), row[0]),
        ).rowcount
    interrupted_review = _canonical_json({
        "extract_failed": True,
        "reason": "application_restart_interrupted",
    })
    reviews_recovered = conn.execute(
        "UPDATE journal SET derivation_json = ?, state = 'failed', revision = revision + 1 "
        "WHERE kind = 'review' AND state = 'pending' "
        "AND NOT EXISTS (SELECT 1 FROM journal confirmation "
        "WHERE confirmation.kind = 'correction' "
        "AND confirmation.parent_journal_id = journal.id "
        "AND confirmation.state = 'awaiting_user' "
        "AND json_extract(CASE WHEN json_valid(confirmation.extraction_json) "
        "THEN confirmation.extraction_json ELSE '{}' END, '$.operation_type') "
        "= 'review_record' "
        "AND json_extract(CASE WHEN json_valid(confirmation.derivation_json) "
        "THEN confirmation.derivation_json ELSE '{}' END, '$.operation.action') "
        "= 'pending_confirmation')",
        (interrupted_review,),
    ).rowcount
    return {"operations": operations_recovered, "reviews": reviews_recovered}


def prepare_review_record_undo_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict:
    """Create the existing high-risk Review Undo proposal for one exact receipt target."""
    operation = get_review_record_operation(db_path, user_id, operation_id)
    if operation is None:
        raise ReviewRecordOperationNotFound("复盘记录操作不存在")
    if operation["state"] != "completed" or operation["outcome"] != "applied":
        raise ReviewRecordOperationConflict("只有已发布的复盘记录才能生成整条撤销预览")
    from ..undo import ReviewOperationConflict, prepare_review_undo_operation

    try:
        return prepare_review_undo_operation(
            db_path,
            user_id,
            journal_id=operation["target_journal_id"],
            expected_revision=operation["result"]["target_revision"],
        )
    except ReviewOperationConflict as error:
        raise ReviewRecordOperationConflict(str(error)) from error
