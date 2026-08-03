"""Read Review timeline-entry edit operations and undo command receipts."""

from __future__ import annotations

import json
from sqlite3 import Connection
from uuid import UUID

from pydantic import ValidationError

from .....platform.database import read_connection
from ..edit_models import (
    REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION,
    ReviewTimelineEntryEditOperationDTO,
    ReviewTimelineEntryEditProposal,
    ReviewTimelineEntryEditReceipt,
    ReviewTimelineEntryEditUndoCommandError,
    ReviewTimelineEntryEditUndoCommandStatus,
)
from .bundle import _load_bundle
from .errors import ReviewTimelineEntryEditOperationConflict, _EditTargetMissing, _UnsafeEditDependency
from .validate import _canonical_uuid, _proposal_digest


def _operation_row(conn: Connection, user_id: str, operation_id: str):
    return conn.execute(
        "SELECT id, operation_id, created_time, extraction_json, derivation_json, "
        "state, revision, parent_journal_id FROM journal "
        "WHERE user_id = ? AND kind = 'correction' AND operation_id = ?",
        (user_id, operation_id),
    ).fetchone()


def _proposal(raw: str | None) -> ReviewTimelineEntryEditProposal:
    try:
        return ReviewTimelineEntryEditProposal.model_validate(json.loads(raw or ""))
    except (json.JSONDecodeError, TypeError, ValidationError) as error:
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正 proposal 已损坏") from error


def _operation_payload(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError) as error:
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正回执已损坏") from error
    if not isinstance(value, dict) or not isinstance(value.get("operation"), dict):
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正回执已损坏")
    return value["operation"]


def _dto(conn: Connection, user_id: str, row) -> dict:
    if row is None:
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正操作不存在")
    proposal = _proposal(row[3])
    if (
        row[1] is None
        or row[7] != proposal.target.journal_id
        or row[5] != "applied"
    ):
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正身份已损坏")
    operation = _operation_payload(row[4])
    if (
        operation.get("type") != "review_timeline_entry_edit"
        or operation.get("client_turn_id") != proposal.client_turn_id
        or operation.get("proposal_digest") != _proposal_digest(proposal)
    ):
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正回执身份已损坏")
    action = operation.get("action")
    if action not in {"complete", "undone"}:
        return ReviewTimelineEntryEditOperationDTO(
            operation_id=row[1],
            operation_type="review_timeline_entry_edit",
            contract_version=REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION,
            state="stale",
            created_time=row[2],
            client_turn_id=proposal.client_turn_id,
            request_digest=proposal.request_digest,
            target=proposal.target,
            before=proposal.before,
            final=proposal.final,
            effect=proposal.effect,
            result=None,
            undo_available=False,
            undo_block_reason="operation_invalid",
        ).model_dump(mode="json")
    try:
        receipt = ReviewTimelineEntryEditReceipt.model_validate(operation.get("result"))
    except ValidationError as error:
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正收据已损坏") from error

    if action == "undone":
        state = "undone"
        undo_available = False
        block_reason = "already_undone"
    else:
        state = "completed"
        try:
            live = _load_bundle(conn, user_id, proposal.target.journal_id)
        except _EditTargetMissing:
            live = None
            block_reason = "target_missing"
        except _UnsafeEditDependency:
            live = None
            block_reason = "target_changed"
        if live is not None and (
            live.target == proposal.target
            and live.entry == proposal.final
            and live.application == proposal.effect.application_final
            and live.fingerprint == proposal.effect.final_dependency_fingerprint
        ):
            undo_available = True
            block_reason = None
        else:
            undo_available = False
            if live is not None:
                block_reason = "target_changed"
    return ReviewTimelineEntryEditOperationDTO(
        operation_id=row[1],
        operation_type="review_timeline_entry_edit",
        contract_version=REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION,
        state=state,
        created_time=row[2],
        client_turn_id=proposal.client_turn_id,
        request_digest=proposal.request_digest,
        target=proposal.target,
        before=proposal.before,
        final=proposal.final,
        effect=proposal.effect,
        result=receipt,
        undo_available=undo_available,
        undo_block_reason=block_reason,
    ).model_dump(mode="json")


def get_review_timeline_entry_edit_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict | None:
    canonical = _canonical_uuid(operation_id, label="operation_id")
    with read_connection(db_path) as conn:
        row = _operation_row(conn, user_id, canonical)
        return None if row is None else _dto(conn, user_id, row)


def list_review_timeline_entry_edit_operations_for_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str | UUID,
) -> list[dict]:
    canonical = _canonical_uuid(client_turn_id, label="client_turn_id")
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, operation_id, created_time, extraction_json, derivation_json, "
            "state, revision, parent_journal_id FROM journal "
            "WHERE user_id = ? AND kind = 'correction' AND operation_id IS NOT NULL "
            "AND json_valid(extraction_json) "
            "AND json_extract(extraction_json, '$.operation_type') = 'review_timeline_entry_edit' "
            "AND json_extract(extraction_json, '$.client_turn_id') = ? ORDER BY id",
            (user_id, canonical),
        ).fetchall()
        return [_dto(conn, user_id, row) for row in rows]


def _undo_command_status_from_row(command_id: str, row) -> dict:
    try:
        extraction = json.loads(row[0] or "")
    except (json.JSONDecodeError, TypeError) as error:
        raise ReviewTimelineEntryEditOperationConflict("复盘修正撤销命令已损坏") from error
    if (
        row[3] != "applied"
        or not isinstance(extraction, dict)
        or set(extraction) != {"operation_type", "target_operation_id"}
        or extraction.get("operation_type") != "review_timeline_entry_edit_undo"
    ):
        raise ReviewTimelineEntryEditOperationConflict("复盘修正撤销命令已损坏")
    try:
        target_operation_id = _canonical_uuid(
            extraction["target_operation_id"],
            label="target_operation_id",
        )
        operation = _operation_payload(row[1])
        action = operation.get("action")
        if operation.get("type") != "review_timeline_entry_edit_undo":
            raise ValueError("undo command family mismatch")
        if action == "completed":
            if set(operation) != {"type", "action"}:
                raise ValueError("completed undo command shape mismatch")
            status = ReviewTimelineEntryEditUndoCommandStatus(
                command_id=command_id,
                operation_id=target_operation_id,
                state="completed",
                terminal=True,
                finished_time=row[2],
            )
        elif action == "rejected":
            if set(operation) != {"type", "action", "code", "message"}:
                raise ValueError("rejected undo command shape mismatch")
            status = ReviewTimelineEntryEditUndoCommandStatus(
                command_id=command_id,
                operation_id=target_operation_id,
                state="rejected",
                terminal=True,
                error=ReviewTimelineEntryEditUndoCommandError(
                    code=operation["code"],
                    message=operation["message"],
                ),
                finished_time=row[2],
            )
        else:
            raise ValueError("undo command action mismatch")
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ReviewTimelineEntryEditOperationConflict("复盘修正撤销命令已损坏") from error
    return status.model_dump(mode="json")


def get_review_timeline_entry_edit_undo_command_status(
    db_path: str,
    user_id: str,
    command_id: str | UUID,
) -> dict:
    canonical = _canonical_uuid(command_id, label="command_id")
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT extraction_json, derivation_json, processed_time, state FROM journal "
            "WHERE user_id = ? AND kind = 'correction' AND operation_id = ?",
            (user_id, canonical),
        ).fetchone()
    if row is None:
        return ReviewTimelineEntryEditUndoCommandStatus(
            command_id=canonical,
            state="absent",
            terminal=False,
        ).model_dump(mode="json")
    return _undo_command_status_from_row(canonical, row)
