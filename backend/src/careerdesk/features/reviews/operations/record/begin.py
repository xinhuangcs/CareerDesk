"""Claim insertion, replay matching and the durable begin phase."""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection
from uuid import uuid4

from .....platform.database import now_iso, transaction
from ..record_models import (
    MAX_PENDING_REVIEW_RECORD_CONFIRMATIONS,
    MAX_REVIEW_RECORD_OPERATIONS_PER_TURN,
    MAX_REVIEW_RECORD_SUPPLEMENTS,
    REVIEW_RECORD_CONTRACT_VERSION,
    ReviewRecordProposal,
)
from .dto import _dto, _operation_rows_for_turn
from .errors import ReviewRecordOperationConflict, ReviewRecordOperationNotFound
from .finalize import _supersede_operation_in_transaction
from .rows import (
    _OPERATION_COLUMNS,
    _bounded_combined,
    _canonical_json,
    _operation_row,
    _proposal,
    _proposal_digest,
    _supplement_rows,
    _text_digest,
)


@dataclass(frozen=True, slots=True)
class _Claim:
    execute: bool
    operation_id: str
    proposal: ReviewRecordProposal
    combined_text: str | None
    dto: dict


def _insert_operation_row(
    conn: Connection,
    user_id: str,
    proposal: ReviewRecordProposal,
    timestamp: str,
) -> None:
    proposal_hash = _proposal_digest(proposal)
    derivation = {"operation": {
        "type": "review_record",
        "action": "processing",
        "client_turn_id": proposal.client_turn_id,
        "proposal_digest": proposal_hash,
    }}
    conn.execute(
        "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
        "derivation_json, state, parent_journal_id, operation_id) "
        "VALUES (?, 'correction', ?, ?, ?, ?, 'pending', ?, ?)",
        (
            user_id,
            f"[record_review operation for #{proposal.target_journal_id}]",
            timestamp,
            _canonical_json(proposal.model_dump(mode="json")),
            _canonical_json(derivation),
            proposal.target_journal_id,
            proposal.operation_id,
        ),
    )


def _matching_replay(
    conn: Connection,
    user_id: str,
    row,
    *,
    client_turn_id: str,
    request_digest: str,
) -> _Claim:
    proposal = _proposal(row[5])
    if (
        proposal.client_turn_id != client_turn_id
        or proposal.request_digest != request_digest
    ):
        raise ReviewRecordOperationConflict(
            "operation_id 或 client_turn_id 已用于另一条 record_review 命令",
        )
    dto = _dto(conn, user_id, row)
    return _Claim(
        execute=False,
        operation_id=proposal.operation_id,
        proposal=proposal,
        combined_text=None,
        dto=dto,
    )


def _begin_operation(
    db_path: str,
    user_id: str,
    *,
    operation_id: str,
    client_turn_id: str,
    text: str,
    review_reference: str | None,
    effective_date: str,
    request_digest: str,
) -> _Claim:
    timestamp = now_iso()
    attempt_token = str(uuid4())
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        own_operation = _operation_row(conn, user_id, operation_id)
        turn_rows = _operation_rows_for_turn(conn, user_id, client_turn_id)
        if own_operation is not None:
            if not any(row[0] == own_operation[0] for row in turn_rows):
                raise ReviewRecordOperationConflict("operation 与 turn 映射发生冲突")
            return _matching_replay(
                conn,
                user_id,
                own_operation,
                client_turn_id=client_turn_id,
                request_digest=request_digest,
            )
        for turn_row in turn_rows:
            proposal = _proposal(turn_row[5])
            if proposal.request_digest == request_digest:
                return _matching_replay(
                    conn,
                    user_id,
                    turn_row,
                    client_turn_id=client_turn_id,
                    request_digest=request_digest,
                )
        if len(turn_rows) >= MAX_REVIEW_RECORD_OPERATIONS_PER_TURN:
            raise ReviewRecordOperationConflict("同一轮复盘条目已达到安全上限")
        if conn.execute(
            "SELECT 1 FROM journal WHERE operation_id = ? LIMIT 1",
            (operation_id,),
        ).fetchone() is not None:
            # Do not reveal whether another tenant or operation family owns the UUID.
            raise ReviewRecordOperationNotFound("复盘记录操作不存在")

        if review_reference is None:
            if conn.execute(
                "SELECT 1 FROM journal target WHERE target.user_id = ? "
                "AND target.kind = 'review' AND target.content = ? AND ("
                "target.state = 'awaiting_user' OR (target.state = 'pending' "
                "AND EXISTS (SELECT 1 FROM journal owner WHERE owner.user_id = target.user_id "
                "AND owner.kind = 'correction' AND owner.parent_journal_id = target.id "
                "AND owner.operation_id IS NOT NULL "
                "AND owner.state IN ('pending', 'awaiting_user') AND ("
                "json_extract(CASE WHEN json_valid(owner.extraction_json) "
                "THEN owner.extraction_json ELSE '{}' END, '$.operation_type') "
                "= 'review_record' OR json_extract(CASE WHEN "
                "json_valid(owner.derivation_json) THEN owner.derivation_json ELSE '{}' END, "
                "'$.operation.type') = 'review_record')))) LIMIT 1",
                (user_id, text),
            ).fetchone() is not None:
                raise ReviewRecordOperationConflict(
                    "相同原文已有一条复盘草稿；如需补充，请使用页面绑定的可选补充入口，"
                    "不要重新创建相同记录。",
                )
            source_cursor = conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, state) "
                "VALUES (?, 'review', ?, ?, 'pending')",
                (user_id, text, timestamp),
            )
            target_journal_id = source_cursor.lastrowid
            source_journal_id = target_journal_id
            combined_text = text
            canonical_reference = operation_id
            target_revision = 0
            mode = "initial"
        else:
            reference_row = _operation_row(conn, user_id, review_reference)
            if reference_row is None:
                raise ReviewRecordOperationNotFound("待补充的复盘引用不存在")
            reference_dto = _dto(conn, user_id, reference_row)
            pending_confirmation = reference_dto["state"] == "pending_confirmation"
            completed_clarification = (
                reference_dto["state"] == "completed"
                and reference_dto["outcome"] == "needs_clarification"
            )
            if not pending_confirmation and not completed_clarification:
                raise ReviewRecordOperationConflict("该复盘引用当前不接受补充")
            canonical_reference = reference_dto["review_reference"]
            target_journal_id = reference_dto["target_journal_id"]
            target = conn.execute(
                "SELECT content, state, revision FROM journal WHERE user_id = ? AND id = ? "
                "AND kind = 'review'",
                (user_id, target_journal_id),
            ).fetchone()
            if target is None:
                raise ReviewRecordOperationNotFound("待补充的复盘不存在")
            original, target_state, expected_revision = target
            if pending_confirmation:
                if (
                    target_state != reference_dto["target_current_state"]
                    or expected_revision != reference_dto["target_current_revision"]
                    or target_state not in {"pending", "awaiting_user"}
                ):
                    raise ReviewRecordOperationConflict("该复盘草稿已变化，不能继续补充")
            elif target_state != "awaiting_user":
                raise ReviewRecordOperationConflict("该复盘已不再接受可选补充")

            live_rows = conn.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM journal WHERE user_id = ? "
                "AND kind = 'correction' AND parent_journal_id = ? "
                "AND operation_id IS NOT NULL AND state IN ('pending', 'awaiting_user') "
                "AND (json_extract(CASE WHEN json_valid(extraction_json) "
                "THEN extraction_json ELSE '{}' END, '$.operation_type') "
                "= 'review_record' OR json_extract(CASE WHEN json_valid(derivation_json) "
                "THEN derivation_json ELSE '{}' END, '$.operation.type') "
                "= 'review_record') ORDER BY id LIMIT ?",
                (
                    user_id,
                    target_journal_id,
                    MAX_PENDING_REVIEW_RECORD_CONFIRMATIONS + 1,
                ),
            ).fetchall()
            if len(live_rows) > MAX_PENDING_REVIEW_RECORD_CONFIRMATIONS:
                raise ReviewRecordOperationConflict("同一复盘存在过多未完成操作")
            for live_row in live_rows:
                live_dto = _dto(conn, user_id, live_row)
                if (
                    live_dto["state"] not in {"processing", "pending_confirmation"}
                    or live_dto["target_journal_id"] != target_journal_id
                    or live_dto["review_reference"] != canonical_reference
                ):
                    raise ReviewRecordOperationConflict("同一复盘的未完成操作身份已损坏")
                _supersede_operation_in_transaction(
                    conn,
                    user_id,
                    live_row,
                    _proposal(live_row[5]),
                    message="这份未完成复盘方案已由用户的新补充取代。",
                )
            existing_supplements = _supplement_rows(conn, user_id, target_journal_id)
            if len(existing_supplements) >= MAX_REVIEW_RECORD_SUPPLEMENTS:
                raise ValueError("该复盘的补充次数已达安全上限")
            combined_text = _bounded_combined(
                original,
                [*(row[1] for row in existing_supplements), text],
            )
            parent = conn.execute(
                "UPDATE journal SET revision = revision + 1 WHERE user_id = ? AND id = ? "
                "AND kind = 'review' AND state = ? AND revision = ? "
                "RETURNING revision",
                (user_id, target_journal_id, target_state, expected_revision),
            ).fetchone()
            if parent is None:  # pragma: no cover - BEGIN IMMEDIATE invariant
                raise ReviewRecordOperationConflict("复盘补充 revision 已变化")
            target_revision = parent[0]
            source_cursor = conn.execute(
                "INSERT INTO journal (user_id, kind, content, created_time, processed_time, "
                "derivation_json, state, parent_journal_id) "
                "VALUES (?, 'correction', ?, ?, ?, ?, 'applied', ?)",
                (
                    user_id,
                    text,
                    timestamp,
                    timestamp,
                    _canonical_json({
                        "merged_into": target_journal_id,
                        "source_type": "review_supplement",
                    }),
                    target_journal_id,
                ),
            )
            source_journal_id = source_cursor.lastrowid
            mode = "supplement"

        proposal = ReviewRecordProposal(
            operation_type="review_record",
            contract_version=REVIEW_RECORD_CONTRACT_VERSION,
            operation_id=operation_id,
            review_reference=canonical_reference,
            client_turn_id=client_turn_id,
            request_digest=request_digest,
            source_digest=_text_digest(text),
            combined_digest=_text_digest(combined_text),
            attempt_token=attempt_token,
            mode=mode,
            effective_date=effective_date,
            source_journal_id=source_journal_id,
            target_journal_id=target_journal_id,
            target_expected_state=("pending" if mode == "initial" else target_state),
            target_expected_revision=target_revision,
        )
        _insert_operation_row(conn, user_id, proposal, timestamp)
        inserted = _operation_row(conn, user_id, operation_id)
        if inserted is None:  # pragma: no cover
            raise RuntimeError("record_review operation insert lost")
        dto = _dto(conn, user_id, inserted)
        return _Claim(
            execute=True,
            operation_id=operation_id,
            proposal=proposal,
            combined_text=combined_text,
            dto=dto,
        )
