"""Conditional undo for Review timeline-entry edit operations."""

from __future__ import annotations

from uuid import UUID

from .....platform.database import now_iso, transaction
from ..edit_models import (
    ReviewTimelineEntryEditReceipt,
    ReviewTimelineEntryEditUndoResult,
)
from .errors import (
    ReviewTimelineEntryEditOperationConflict,
    ReviewTimelineEntryEditOperationNotFound,
)
from .projection import _apply_entry_projection_in_transaction
from .read import (
    _dto,
    _operation_payload,
    _operation_row,
    _proposal,
    _undo_command_status_from_row,
)
from .validate import _canonical_json, _canonical_uuid, _proposal_digest


def _write_command_receipt(
    conn,
    user_id: str,
    command_id: str,
    operation_id: str,
    *,
    action: str,
    code: str | None = None,
    message: str | None = None,
) -> None:
    timestamp = now_iso()
    payload = {"operation": {"type": "review_timeline_entry_edit_undo", "action": action}}
    if code is not None:
        payload["operation"]["code"] = code
    if message is not None:
        payload["operation"]["message"] = message
    conn.execute(
        "INSERT INTO journal "
        "(user_id, kind, content, created_time, processed_time, extraction_json, "
        "derivation_json, state, operation_id) "
        "VALUES (?, 'correction', ?, ?, ?, ?, ?, 'applied', ?)",
        (
            user_id,
            "[撤销复盘历程修正]",
            timestamp,
            timestamp,
            _canonical_json({
                "operation_type": "review_timeline_entry_edit_undo",
                "target_operation_id": operation_id,
            }),
            _canonical_json(payload),
            command_id,
        ),
    )


def undo_review_timeline_entry_edit_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
    *,
    command_id: str | UUID,
) -> dict:
    canonical_operation = _canonical_uuid(operation_id, label="operation_id")
    canonical_command = _canonical_uuid(command_id, label="command_id")
    deferred_conflict: str | None = None
    completed: dict | None = None
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        command_row = conn.execute(
            "SELECT extraction_json, derivation_json, processed_time, state FROM journal "
            "WHERE user_id = ? AND kind = 'correction' AND operation_id = ?",
            (user_id, canonical_command),
        ).fetchone()
        if command_row is not None:
            command_status = _undo_command_status_from_row(
                canonical_command,
                command_row,
            )
            if command_status["operation_id"] != canonical_operation:
                raise ReviewTimelineEntryEditOperationConflict(
                    "撤销命令已绑定另一笔复盘修正",
                )
            if command_status["state"] == "rejected":
                raise ReviewTimelineEntryEditOperationConflict(
                    command_status["error"]["message"],
                )
            row = _operation_row(conn, user_id, canonical_operation)
            if row is None:
                raise ReviewTimelineEntryEditOperationNotFound("复盘历程修正操作不存在")
            return _dto(conn, user_id, row)

        row = _operation_row(conn, user_id, canonical_operation)
        if row is None:
            raise ReviewTimelineEntryEditOperationNotFound("复盘历程修正操作不存在")
        try:
            current_dto = _dto(conn, user_id, row)
        except ReviewTimelineEntryEditOperationConflict:
            current_dto = None
            deferred_conflict = "复盘历程修正收据已损坏，不能执行撤销"
            _write_command_receipt(
                conn,
                user_id,
                canonical_command,
                canonical_operation,
                action="rejected",
                code="operation_invalid",
                message=deferred_conflict,
            )
        if current_dto is None:
            pass
        elif current_dto["state"] == "undone":
            _write_command_receipt(
                conn,
                user_id,
                canonical_command,
                canonical_operation,
                action="completed",
            )
            return current_dto
        elif not current_dto["undo_available"]:
            reason = current_dto.get("undo_block_reason") or "target_changed"
            deferred_conflict = "复盘历程或岗位投影已变化，不能覆盖后续修改"
            _write_command_receipt(
                conn,
                user_id,
                canonical_command,
                canonical_operation,
                action="rejected",
                code=reason if reason in {
                    "target_missing", "target_changed", "provenance_changed", "operation_invalid",
                } else "operation_invalid",
                message=deferred_conflict,
            )
        else:
            proposal = _proposal(row[3])
            from .bundle import _load_bundle

            live = _load_bundle(conn, user_id, proposal.target.journal_id)
            values = {
                field: getattr(proposal.before, field)
                for field in proposal.effect.changed_fields
            }
            timestamp = now_iso()
            write = _apply_entry_projection_in_transaction(
                conn,
                user_id,
                live,
                values=values,
                changed_fields=set(proposal.effect.changed_fields),
                timestamp=timestamp,
            )
            old_operation = _operation_payload(row[4])
            original_receipt = ReviewTimelineEntryEditReceipt.model_validate(
                old_operation["result"],
            )
            receipt = ReviewTimelineEntryEditReceipt(
                apply=original_receipt.apply,
                undo=ReviewTimelineEntryEditUndoResult(
                    status="ok",
                    journal_id=proposal.target.journal_id,
                    timeline_entry_id=proposal.target.timeline_entry_id,
                    application_id=proposal.target.application_id,
                    target_revision=write.entry.journal_revision,
                    application_revision=write.application.revision,
                    timeline_entries_updated=1,
                    occurrences_updated=len(write.occurrence_effects),
                    status_logs_updated=len(write.status_effects),
                    application_updated=True,
                ),
            )
            changed = conn.execute(
                "UPDATE journal SET derivation_json = ?, processed_time = ?, revision = revision + 1 "
                "WHERE user_id = ? AND id = ? AND kind = 'correction' AND operation_id = ? "
                "AND revision = ? AND derivation_json = ?",
                (
                    _canonical_json({"operation": {
                        "type": "review_timeline_entry_edit",
                        "action": "undone",
                        "client_turn_id": proposal.client_turn_id,
                        "proposal_digest": _proposal_digest(proposal),
                        "result": receipt.model_dump(mode="json"),
                    }}),
                    timestamp,
                    user_id,
                    row[0],
                    canonical_operation,
                    row[6],
                    row[4],
                ),
            ).rowcount
            if changed != 1:
                raise ReviewTimelineEntryEditOperationConflict("复盘历程修正收据已变化")
            _write_command_receipt(
                conn,
                user_id,
                canonical_command,
                canonical_operation,
                action="completed",
            )
            completed = _dto(
                conn,
                user_id,
                _operation_row(conn, user_id, canonical_operation),
            )
    if deferred_conflict is not None:
        raise ReviewTimelineEntryEditOperationConflict(deferred_conflict)
    if completed is None:  # pragma: no cover - every non-conflict path returns a DTO.
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正撤销未完成")
    return completed
