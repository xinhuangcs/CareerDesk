"""Operation DTO derivation, snapshot validation and per-turn row loading."""

from __future__ import annotations

from sqlite3 import Connection

from pydantic import ValidationError

from ....journal import public as journal
from ..record_models import (
    MAX_REVIEW_RECORD_OPERATIONS_PER_TURN,
    REVIEW_RECORD_CONTRACT_VERSION,
    ReviewRecordOperationDTO,
    ReviewRecordOperationError,
    ReviewRecordPreview,
    ReviewRecordProposal,
    ReviewRecordResult,
)
from .errors import (
    ReviewRecordOperationConflict,
    ReviewRecordOperationNotFound,
    _UnsafeRecordDependency,
)
from .rows import (
    MAX_TURN_OPERATION_CANDIDATES,
    _bounded_combined,
    _canonical_json,
    _classify_turn_candidate,
    _object_json,
    _operation_row,
    _proposal,
    _proposal_digest,
    _supplement_rows,
    _text_digest,
    _turn_candidate_family,
)


def _operation_rows_for_turn(conn: Connection, user_id: str, client_turn_id: str) -> list:
    candidates = journal.read_operation_candidates_for_turn_in_transaction(
        conn,
        user_id,
        client_turn_id,
        maximum=MAX_TURN_OPERATION_CANDIDATES,
    )
    if len(candidates) > MAX_TURN_OPERATION_CANDIDATES:
        raise ReviewRecordOperationConflict("同一轮 operation 候选超过安全上限")
    rows = []
    for candidate in candidates:
        family = _classify_turn_candidate(
            operation_id=candidate.operation_id,
            kind=candidate.kind,
            extraction_json=candidate.extraction_json,
            derivation_json=candidate.derivation_json,
            client_turn_id=client_turn_id,
        )
        if family != "review_record":
            # Other known operation families may legitimately share one Assistant turn.
            continue
        row = _operation_row(conn, user_id, candidate.operation_id)
        if row is None or row[0] != candidate.journal_id:
            raise ReviewRecordOperationConflict(
                "同轮复盘记录 operation 身份已损坏",
            )
        _dto(conn, user_id, row)
        rows.append(row)
    if len(rows) > MAX_REVIEW_RECORD_OPERATIONS_PER_TURN:
        raise ReviewRecordOperationConflict("同一轮存在多条 record_review 操作")
    return rows


def _terminal_operation(raw: str | None) -> dict:
    payload = _object_json(raw, "复盘操作回执")
    if set(payload) != {"operation"} or not isinstance(payload["operation"], dict):
        raise ReviewRecordOperationConflict("复盘操作回执 envelope 已损坏")
    return payload["operation"]


def _pending_confirmation_preview(
    raw: str | None,
    proposal: ReviewRecordProposal,
) -> ReviewRecordPreview:
    operation = _terminal_operation(raw)
    if (
        set(operation) != {
            "type",
            "action",
            "client_turn_id",
            "proposal_digest",
            "extraction",
            "target_plan",
            "missing",
        }
        or operation.get("type") != "review_record"
        or operation.get("action") != "pending_confirmation"
        or operation.get("client_turn_id") != proposal.client_turn_id
        or operation.get("proposal_digest") != _proposal_digest(proposal)
    ):
        raise ReviewRecordOperationConflict("待确认复盘预览身份已损坏")
    try:
        preview = ReviewRecordPreview.model_validate({
            "extraction": operation["extraction"],
            "target_plan": operation["target_plan"],
            "missing": operation["missing"],
        })
    except (TypeError, ValueError, ValidationError) as error:
        raise ReviewRecordOperationConflict("待确认复盘预览内容已损坏") from error
    expected = {
        "type": "review_record",
        "action": "pending_confirmation",
        "client_turn_id": proposal.client_turn_id,
        "proposal_digest": _proposal_digest(proposal),
        **preview.model_dump(mode="json"),
    }
    if _canonical_json(operation) != _canonical_json(expected):
        raise ReviewRecordOperationConflict("待确认复盘预览不是规范持久形态")
    return preview


def _validate_source_snapshot(
    conn: Connection,
    user_id: str,
    proposal: ReviewRecordProposal,
    *,
    allow_voided_supplement: bool = False,
) -> None:
    source = conn.execute(
        "SELECT user_id, kind, parent_journal_id, operation_id, content, state "
        "FROM journal WHERE id = ?",
        (proposal.source_journal_id,),
    ).fetchone()
    expected_kind = "review" if proposal.mode == "initial" else "correction"
    expected_parent = None if proposal.mode == "initial" else proposal.target_journal_id
    if (
        source is None
        or source[0] != user_id
        or source[1] != expected_kind
        or source[2] != expected_parent
        or source[3] is not None
        or not isinstance(source[4], str)
        or _text_digest(source[4]) != proposal.source_digest
        or (
            proposal.mode == "supplement"
            and source[5] not in (
                {"applied", "voided"}
                if allow_voided_supplement
                else {"applied"}
            )
        )
    ):
        raise ReviewRecordOperationConflict("复盘原文快照身份或内容已损坏")
    if proposal.mode == "initial":
        combined = source[4]
    else:
        target = conn.execute(
            "SELECT content FROM journal WHERE user_id = ? AND id = ? AND kind = 'review'",
            (user_id, proposal.target_journal_id),
        ).fetchone()
        if target is None:
            raise ReviewRecordOperationConflict("复盘原文快照目标已损坏")
        try:
            supplements = _supplement_rows(
                conn,
                user_id,
                proposal.target_journal_id,
                through_source_journal_id=proposal.source_journal_id,
            )
            prior_supplements = [
                content
                for journal_id, content in supplements
                if journal_id != proposal.source_journal_id
            ]
            combined = _bounded_combined(
                target[0],
                [*prior_supplements, source[4]],
            )
        except _UnsafeRecordDependency as error:
            raise ReviewRecordOperationConflict("复盘补充前缀超过安全契约") from error
    if _text_digest(combined) != proposal.combined_digest:
        raise ReviewRecordOperationConflict("复盘操作冻结的原文前缀已损坏")


def _dto(conn: Connection, user_id: str, row) -> dict:
    if row is None:
        raise ReviewRecordOperationNotFound("复盘记录操作不存在")
    (
        _journal_id,
        operation_id,
        journal_state,
        created_time,
        processed_time,
        extraction_json,
        derivation_json,
        parent_journal_id,
        revision,
        kind,
    ) = row
    proposal = _proposal(extraction_json)
    if (
        kind != "correction"
        or _turn_candidate_family(row, proposal.client_turn_id) != "review_record"
    ):
        raise ReviewRecordOperationConflict("复盘操作类型身份已损坏")
    if operation_id != proposal.operation_id or parent_journal_id != proposal.target_journal_id:
        raise ReviewRecordOperationConflict("复盘操作与冻结目标身份不一致")
    _validate_source_snapshot(
        conn,
        user_id,
        proposal,
        allow_voided_supplement=journal_state == "voided",
    )
    target = conn.execute(
        "SELECT state, revision FROM journal WHERE user_id = ? AND id = ? AND kind = 'review'",
        (user_id, proposal.target_journal_id),
    ).fetchone()
    if (
        target is None
        or target[0] not in {
            "pending", "awaiting_user", "applied", "failed", "superseded", "voided",
        }
        or isinstance(target[1], bool)
        or not isinstance(target[1], int)
        or target[1] < 0
    ):
        raise ReviewRecordOperationConflict("复盘操作的当前目标状态已损坏")
    target_current_state, target_current_revision = target
    operation = _terminal_operation(derivation_json)
    proposal_hash = _proposal_digest(proposal)
    common = {
        "operation_type": "review_record",
        "contract_version": REVIEW_RECORD_CONTRACT_VERSION,
        "operation_id": proposal.operation_id,
        "review_reference": proposal.review_reference,
        "client_turn_id": proposal.client_turn_id,
        "mode": proposal.mode,
        "created_time": created_time,
        "source_journal_id": proposal.source_journal_id,
        "target_journal_id": proposal.target_journal_id,
        "target_current_state": target_current_state,
        "target_current_revision": target_current_revision,
    }
    if journal_state == "pending":
        expected = {
            "type": "review_record",
            "action": "processing",
            "client_turn_id": proposal.client_turn_id,
            "proposal_digest": proposal_hash,
        }
        if revision != 0 or processed_time is not None or operation != expected:
            raise ReviewRecordOperationConflict("processing 复盘操作回执已损坏")
        dto = ReviewRecordOperationDTO(
            **common,
            state="processing",
            terminal=False,
            undo_available=False,
            undo_block_reason="operation_not_applied",
        )
        return dto.model_dump(mode="json")

    if journal_state == "awaiting_user":
        preview = _pending_confirmation_preview(derivation_json, proposal)
        if revision != 1 or processed_time is not None:
            raise ReviewRecordOperationConflict("待确认复盘预览生命周期已损坏")
        dto = ReviewRecordOperationDTO(
            **common,
            state="pending_confirmation",
            terminal=False,
            preview=preview,
            undo_available=False,
            undo_block_reason="operation_not_applied",
        )
        return dto.model_dump(mode="json")

    valid_revisions = {
        "applied": {2},
        "failed": {1},
        "superseded": {1, 2},
        "voided": {2},
    }
    if journal_state not in valid_revisions or revision not in valid_revisions[journal_state]:
        raise ReviewRecordOperationConflict("复盘操作处于无法识别的持久状态")
    expected_action = {
        "applied": "complete",
        "failed": "failed",
        "superseded": "superseded",
        "voided": "rejected",
    }[journal_state]
    expected_keys = {
        "type",
        "action",
        "client_turn_id",
        "proposal_digest",
        "finished_time",
    }
    if journal_state == "applied":
        expected_keys.add("result")
    elif journal_state != "voided":
        expected_keys.add("error")
    if (
        set(operation) != expected_keys
        or operation.get("type") != "review_record"
        or operation.get("action") != expected_action
        or operation.get("client_turn_id") != proposal.client_turn_id
        or operation.get("proposal_digest") != proposal_hash
        or operation.get("finished_time") != processed_time
    ):
        raise ReviewRecordOperationConflict("终态复盘操作回执已损坏")

    if journal_state == "applied":
        raw_result = operation["result"]
        try:
            result = ReviewRecordResult.model_validate(raw_result)
        except (TypeError, ValueError, ValidationError) as error:
            raise ReviewRecordOperationConflict("复盘操作成功回执已损坏") from error
        if _canonical_json(result.model_dump(mode="json")) != _canonical_json(raw_result):
            raise ReviewRecordOperationConflict("复盘操作成功回执不是规范持久形态")
        if (
            result.review_reference != proposal.review_reference
            or result.source_journal_id != proposal.source_journal_id
            or result.target_journal_id != proposal.target_journal_id
        ):
            raise ReviewRecordOperationConflict("复盘操作成功回执目标已损坏")
        dto = ReviewRecordOperationDTO(
            **common,
            state="completed",
            terminal=True,
            outcome=result.outcome,
            finished_time=processed_time,
            result=result,
            undo_available=(
                result.outcome == "applied"
                and target_current_state == "applied"
                and target_current_revision == result.target_revision
            ),
            undo_block_reason=(
                "operation_not_applied"
                if result.outcome != "applied"
                else "target_not_applied"
                if target_current_state != "applied"
                else "target_changed"
                if target_current_revision != result.target_revision
                else None
            ),
        )
    elif journal_state == "voided":
        dto = ReviewRecordOperationDTO(
            **common,
            state="rejected",
            terminal=True,
            finished_time=processed_time,
            undo_available=False,
            undo_block_reason="operation_not_applied",
        )
    else:
        try:
            error = ReviewRecordOperationError.model_validate(operation["error"])
        except (TypeError, ValueError, ValidationError) as validation_error:
            raise ReviewRecordOperationConflict("复盘操作失败回执已损坏") from validation_error
        dto = ReviewRecordOperationDTO(
            **common,
            state=journal_state,
            terminal=True,
            finished_time=processed_time,
            error=error,
            undo_available=False,
            undo_block_reason="operation_not_applied",
        )
    return dto.model_dump(mode="json")
