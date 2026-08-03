"""Atomic publication of fully extracted Review batches."""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from .....platform.database import now_iso, transaction
from ...ai_models import MAX_REVIEW_BATCH_ITEMS, ReviewExtraction
from ..record_models import REVIEW_RECORD_CONTRACT_VERSION, ReviewRecordProposal
from .begin import _insert_operation_row
from .dto import _dto, _operation_rows_for_turn
from .errors import ReviewRecordOperationConflict, ReviewRecordOperationNotFound
from .finalize import _stage_operation_for_confirmation_in_transaction
from .rows import (
    _canonical_uuid,
    _proposal,
    _request_digest,
    _text_digest,
    _validate_source_text,
    _validate_user_id,
)


def _canonical_effective_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("effective_date 必须是真实的 YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError("effective_date 必须是规范的 YYYY-MM-DD")
    return value


async def execute_review_record_batch_operations(
    db_path: str,
    user_id: str,
    *,
    client_turn_id: str | UUID,
    effective_date: str,
    items: list[tuple[str | UUID, str, ReviewExtraction]],
) -> list[dict]:
    """Publish every prepared preview in one transaction or publish none."""
    _validate_user_id(user_id)
    canonical_turn = _canonical_uuid(client_turn_id, label="client_turn_id")
    canonical_date = _canonical_effective_date(effective_date)
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_REVIEW_BATCH_ITEMS:
        raise ValueError(f"批量复盘必须包含 1 到 {MAX_REVIEW_BATCH_ITEMS} 项")

    prepared: list[tuple[str, str, ReviewExtraction, str]] = []
    for operation_id, text, extraction in items:
        canonical_operation = _canonical_uuid(operation_id, label="operation_id")
        source_text = _validate_source_text(text)
        validated = ReviewExtraction.model_validate(extraction)
        prepared.append((
            canonical_operation,
            source_text,
            validated,
            _request_digest(canonical_turn, source_text, None),
        ))
    operation_ids = [item[0] for item in prepared]
    source_texts = [item[1] for item in prepared]
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("批量复盘 operation_id 必须唯一")
    if len(source_texts) != len(set(source_texts)):
        raise ValueError("批量复盘 source_text 必须唯一")

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_rows = _operation_rows_for_turn(conn, user_id, canonical_turn)
        if existing_rows:
            existing_by_digest = {
                _proposal(row[5]).request_digest: row for row in existing_rows
            }
            requested_digests = [item[3] for item in prepared]
            if (
                len(existing_rows) != len(prepared)
                or len(existing_by_digest) != len(existing_rows)
                or any(digest not in existing_by_digest for digest in requested_digests)
            ):
                raise ReviewRecordOperationConflict(
                    "同一轮批量复盘与已发布的岗位清单不一致",
                )
            return [
                _dto(conn, user_id, existing_by_digest[digest])
                for digest in requested_digests
            ]

        timestamp = now_iso()
        staged: list[tuple[ReviewRecordProposal, ReviewExtraction]] = []
        for operation_id, source_text, extraction, request_digest in prepared:
            if conn.execute(
                "SELECT 1 FROM journal WHERE operation_id = ? LIMIT 1",
                (operation_id,),
            ).fetchone() is not None:
                raise ReviewRecordOperationNotFound("复盘记录操作不存在")
            if conn.execute(
                "SELECT 1 FROM journal target WHERE target.user_id = ? "
                "AND target.kind = 'review' AND target.content = ? AND ("
                "target.state = 'awaiting_user' OR (target.state = 'pending' "
                "AND EXISTS (SELECT 1 FROM journal owner "
                "WHERE owner.user_id = target.user_id "
                "AND owner.kind = 'correction' "
                "AND owner.parent_journal_id = target.id "
                "AND owner.operation_id IS NOT NULL "
                "AND owner.state IN ('pending', 'awaiting_user')))) LIMIT 1",
                (user_id, source_text),
            ).fetchone() is not None:
                raise ReviewRecordOperationConflict("相同原文已有一条复盘草稿")
            source_cursor = conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, state) "
                "VALUES (?, 'review', ?, ?, 'pending')",
                (user_id, source_text, timestamp),
            )
            target_journal_id = source_cursor.lastrowid
            proposal = ReviewRecordProposal(
                operation_type="review_record",
                contract_version=REVIEW_RECORD_CONTRACT_VERSION,
                operation_id=operation_id,
                review_reference=operation_id,
                client_turn_id=canonical_turn,
                request_digest=request_digest,
                source_digest=_text_digest(source_text),
                combined_digest=_text_digest(source_text),
                attempt_token=str(uuid4()),
                mode="initial",
                effective_date=canonical_date,
                source_journal_id=target_journal_id,
                target_journal_id=target_journal_id,
                target_expected_state="pending",
                target_expected_revision=0,
            )
            _insert_operation_row(conn, user_id, proposal, timestamp)
            staged.append((proposal, extraction))

        return [
            _stage_operation_for_confirmation_in_transaction(
                conn,
                user_id,
                proposal,
                extraction,
            )
            for proposal, extraction in staged
        ]
