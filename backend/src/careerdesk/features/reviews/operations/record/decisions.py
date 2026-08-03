"""Approve, reject and whole-turn batch decisions."""

from __future__ import annotations

from sqlite3 import Connection, IntegrityError
from uuid import UUID

from pydantic import ValidationError

from .....platform.database import (
    application_identity_key,
    normalize_application_identity_part,
    now_iso,
    read_connection,
    transaction,
)
from ...ai_models import ReviewExtraction
from ..record_models import (
    MAX_REVIEW_RECORD_BATCH_DECISION_CHARS,
    MAX_REVIEW_RECORD_OPERATIONS_PER_TURN,
    ReviewRecordDecision,
    ReviewRecordPreview,
    ReviewRecordProposal,
)
from .dto import _dto, _operation_rows_for_turn, _pending_confirmation_preview
from .errors import (
    ReviewRecordOperationConflict,
    ReviewRecordOperationNotFound,
    _UnsafeRecordDependency,
)
from .finalize import (
    _finalize_operation,
    _finalize_operation_in_transaction,
    _missing_fields,
    _pending_confirmation_derivation,
    _resolve_target_plan,
    _source_and_combined,
    _supersede_operation_in_transaction,
    _terminal_derivation,
)
from .rows import (
    _canonical_json,
    _canonical_uuid,
    _operation_row,
    _proposal,
    _validate_user_id,
)


def _batch_application_identity(
    extraction: ReviewExtraction,
) -> tuple[str, str] | None:
    """Return the persisted natural key used by the applications table.

    A partial identity still belongs to the normal clarification path.  A complete
    identity must be canonicalizable before the batch starts writing so two display
    spellings cannot both be planned as ``new`` and collide during finalization.
    """
    if extraction.company is None or extraction.position is None:
        return None
    try:
        return application_identity_key(extraction.company, extraction.position)
    except ValueError as error:
        raise ReviewRecordOperationConflict(
            "批量编辑后的公司或岗位去除空白后不能为空，整批没有执行",
        ) from error


def _is_application_identity_collision(error: IntegrityError) -> bool:
    """Recognize only the persisted applications natural-key constraint."""
    message = str(error)
    return all(
        column in message
        for column in (
            "applications.user_id",
            "applications.company_key",
            "applications.position_key",
        )
    )


def approve_review_record_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict:
    """Publish one exact persisted preview; browser input cannot alter extracted facts."""
    _validate_user_id(user_id)
    canonical = _canonical_uuid(operation_id, label="operation_id")
    with read_connection(db_path) as conn:
        conn.execute("BEGIN")
        row = _operation_row(conn, user_id, canonical)
        if row is None:
            raise ReviewRecordOperationNotFound("复盘记录操作不存在")
        if row[2] != "awaiting_user":
            if row[2] == "pending":
                raise ReviewRecordOperationConflict("复盘仍在提取，暂时不能确认")
            return _dto(conn, user_id, row)
        proposal = _proposal(row[5])
        preview = _pending_confirmation_preview(row[6], proposal)
    return _finalize_operation(db_path, user_id, proposal, preview.extraction)


def _reject_review_record_operation_in_transaction(
    conn: Connection,
    user_id: str,
    operation_id: str | UUID,
) -> dict:
    """Reject one exact preview inside the caller's write transaction."""
    canonical = _canonical_uuid(operation_id, label="operation_id")
    row = _operation_row(conn, user_id, canonical)
    if row is None:
        raise ReviewRecordOperationNotFound("复盘记录操作不存在")
    if row[2] != "awaiting_user":
        if row[2] == "pending":
            raise ReviewRecordOperationConflict("复盘仍在提取，暂时不能放弃")
        return _dto(conn, user_id, row)
    proposal = _proposal(row[5])
    _pending_confirmation_preview(row[6], proposal)
    try:
        source = _source_and_combined(conn, user_id, proposal)
    except _UnsafeRecordDependency:
        source = None
    if source is None:
        return _supersede_operation_in_transaction(
            conn,
            user_id,
            row,
            proposal,
            code="source_changed",
            message="复盘原文、补充或目标 revision 已变化，旧确认卡没有执行。",
        )

    finished = now_iso()
    if proposal.mode == "supplement":
        source_changed = conn.execute(
            "UPDATE journal SET processed_time = ?, derivation_json = ?, state = 'voided', "
            "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'correction' "
            "AND parent_journal_id = ? AND operation_id IS NULL AND state = 'applied' "
            "AND json_extract(CASE WHEN json_valid(derivation_json) THEN derivation_json "
            "ELSE '{}' END, '$.source_type') = 'review_supplement'",
            (
                finished,
                _canonical_json({
                    "discarded": True,
                    "source_type": "review_supplement",
                }),
                user_id,
                proposal.source_journal_id,
                proposal.target_journal_id,
            ),
        ).rowcount
        if source_changed != 1:
            raise ReviewRecordOperationConflict("复盘补充草稿作废发生竞争")

    if proposal.target_expected_state == "pending":
        target_changed = conn.execute(
            "UPDATE journal SET processed_time = ?, derivation_json = ?, state = 'voided', "
            "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'review' "
            "AND state = 'pending' AND revision = ?",
            (
                finished,
                _canonical_json({
                    "record_review_rejected": True,
                    "operation_id": proposal.operation_id,
                }),
                user_id,
                proposal.target_journal_id,
                proposal.target_expected_revision,
            ),
        ).rowcount
    else:
        target_changed = conn.execute(
            "UPDATE journal SET revision = revision + 1 WHERE user_id = ? AND id = ? "
            "AND kind = 'review' AND state = 'awaiting_user' AND revision = ?",
            (
                user_id,
                proposal.target_journal_id,
                proposal.target_expected_revision,
            ),
        ).rowcount
    if target_changed != 1:
        raise ReviewRecordOperationConflict("复盘草稿目标作废发生竞争")

    operation_changed = conn.execute(
        "UPDATE journal SET processed_time = ?, derivation_json = ?, state = 'voided', "
        "revision = revision + 1 WHERE user_id = ? AND id = ? AND kind = 'correction' "
        "AND operation_id = ? AND state = 'awaiting_user' AND revision = 1",
        (
            finished,
            _canonical_json(_terminal_derivation(
                proposal,
                action="rejected",
                finished_time=finished,
            )),
            user_id,
            row[0],
            proposal.operation_id,
        ),
    ).rowcount
    if operation_changed != 1:
        raise ReviewRecordOperationConflict("复盘确认卡作废发生竞争")
    return _dto(conn, user_id, _operation_row(conn, user_id, canonical))


def reject_review_record_operation(
    db_path: str,
    user_id: str,
    operation_id: str | UUID,
) -> dict:
    """Reject one exact preview without creating any business projection."""
    _validate_user_id(user_id)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _reject_review_record_operation_in_transaction(
            conn,
            user_id,
            operation_id,
        )


def reject_review_record_operations_for_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str | UUID,
) -> list[dict]:
    """Reject every pending Review proposal owned by one Assistant turn."""
    _validate_user_id(user_id)
    canonical_turn = _canonical_uuid(client_turn_id, label="client_turn_id")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = _operation_rows_for_turn(conn, user_id, canonical_turn)
        operations = {
            row[1]: _dto(conn, user_id, row)
            for row in rows
        }
        if any(operation["state"] == "processing" for operation in operations.values()):
            raise ReviewRecordOperationConflict("复盘仍在提取，暂时不能取消")
        for operation_id, operation in operations.items():
            if operation["state"] == "pending_confirmation":
                _reject_review_record_operation_in_transaction(
                    conn,
                    user_id,
                    operation_id,
                )
        return [
            _dto(conn, user_id, _operation_row(conn, user_id, row[1]))
            for row in rows
        ]


def decide_review_record_operations_for_turn(
    db_path: str,
    user_id: str,
    client_turn_id: str | UUID,
    decisions: list[ReviewRecordDecision | dict],
) -> list[dict]:
    """Apply one complete Review batch decision in a single write transaction.

    Every currently pending child in the turn must be present.  Already-terminal
    children are accepted only when they match the same decision, which makes an
    uncertain HTTP response safely replayable without weakening the batch boundary.
    """
    _validate_user_id(user_id)
    canonical_turn = _canonical_uuid(client_turn_id, label="client_turn_id")
    if not isinstance(decisions, list) or not (
        1 <= len(decisions) <= MAX_REVIEW_RECORD_OPERATIONS_PER_TURN
    ):
        raise ValueError(
            "decisions 必须包含 1–"
            f"{MAX_REVIEW_RECORD_OPERATIONS_PER_TURN} 条复盘决定",
        )
    try:
        normalized = [
            decision
            if isinstance(decision, ReviewRecordDecision)
            else ReviewRecordDecision.model_validate(decision)
            for decision in decisions
        ]
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("decisions 不符合复盘批量决定契约") from error
    decision_by_id = {decision.operation_id: decision for decision in normalized}
    if len(decision_by_id) != len(normalized):
        raise ValueError("decisions 不能包含重复的 operation_id")
    edited_chars = sum(
        len(decision.edited_extraction.model_dump_json())
        for decision in normalized
        if decision.edited_extraction is not None
    )
    if edited_chars > MAX_REVIEW_RECORD_BATCH_DECISION_CHARS:
        raise ValueError("批量岗位编辑内容超过安全上限")

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = _operation_rows_for_turn(conn, user_id, canonical_turn)
        if not rows:
            raise ReviewRecordOperationNotFound("本轮没有复盘记录操作")
        row_by_id = {row[1]: row for row in rows}
        if not set(decision_by_id).issubset(row_by_id):
            raise ReviewRecordOperationConflict("批量决定包含不属于本轮的复盘操作")

        current_by_id = {
            operation_id: _dto(conn, user_id, row)
            for operation_id, row in row_by_id.items()
        }
        pending_ids = {
            operation_id
            for operation_id, operation in current_by_id.items()
            if operation["state"] == "pending_confirmation"
        }
        if not pending_ids.issubset(decision_by_id):
            raise ReviewRecordOperationConflict("批量决定遗漏了本轮仍待确认的复盘操作")

        for operation_id, decision in decision_by_id.items():
            operation = current_by_id[operation_id]
            if operation["state"] == "pending_confirmation":
                continue
            expected_state = "completed" if decision.action == "approve" else "rejected"
            if operation["state"] != expected_state:
                raise ReviewRecordOperationConflict(
                    "批量决定与已经完成的复盘操作不一致",
                )
            if (
                decision.action == "approve"
                and decision.edited_extraction is not None
                and operation.get("result", {}).get("extraction")
                != decision.edited_extraction.model_dump(mode="json")
            ):
                raise ReviewRecordOperationConflict(
                    "批量重试携带的岗位编辑与已完成结果不一致",
                )

        prepared_approvals: dict[
            str,
            tuple[ReviewRecordProposal, ReviewExtraction, ReviewRecordPreview],
        ] = {}
        approved_identities: set[tuple[str, str]] = set()
        approved_application_ids: set[int] = set()
        for row in rows:
            operation_id = row[1]
            decision = decision_by_id.get(operation_id)
            if (
                decision is None
                or decision.action != "approve"
                or current_by_id[operation_id]["state"] != "pending_confirmation"
            ):
                continue
            proposal = _proposal(row[5])
            preview = _pending_confirmation_preview(row[6], proposal)
            try:
                source = _source_and_combined(conn, user_id, proposal)
            except _UnsafeRecordDependency as error:
                raise ReviewRecordOperationConflict(
                    "复盘原文或补充已变化，整批没有执行",
                ) from error
            try:
                live_target_plan = _resolve_target_plan(
                    conn,
                    user_id,
                    preview.extraction,
                )
            except ValueError as error:
                raise ReviewRecordOperationConflict(
                    "复盘岗位身份无效，整批没有执行",
                ) from error
            if source is None or live_target_plan != preview.target_plan:
                raise ReviewRecordOperationConflict(
                    "岗位归属或状态在统一确认前已变化，整批没有执行",
                )
            extraction = decision.edited_extraction or preview.extraction
            if preview.target_plan is not None and decision.edited_extraction is not None:
                identity_changed = any(
                    normalize_application_identity_part(getattr(extraction, field))
                    != normalize_application_identity_part(
                        getattr(preview.extraction, field),
                    )
                    for field in ("company", "position")
                )
                if identity_changed:
                    raise ReviewRecordOperationConflict(
                        "已锁定本次复盘的岗位；身份识别错误时请排除并重新复盘",
                    )
            try:
                target_plan = _resolve_target_plan(conn, user_id, extraction)
            except ValueError as error:
                raise ReviewRecordOperationConflict(
                    "批量编辑后的复盘岗位身份无效，整批没有执行",
                ) from error
            try:
                final_preview = ReviewRecordPreview(
                    extraction=extraction,
                    target_plan=target_plan,
                    missing=_missing_fields(conn, user_id, extraction, target_plan),
                )
            except ValidationError as error:
                raise ReviewRecordOperationConflict(
                    "批量编辑后的复盘没有可发布的有效进展",
                ) from error
            identity = _batch_application_identity(extraction)
            if identity is not None:
                if identity in approved_identities:
                    raise ReviewRecordOperationConflict(
                        "批量编辑产生了重复的公司和岗位，整批没有执行",
                    )
                approved_identities.add(identity)
            if target_plan is not None and target_plan.kind == "existing":
                if target_plan.application_id in approved_application_ids:
                    raise ReviewRecordOperationConflict(
                        "批量中的多条进展指向同一个岗位，整批没有执行",
                    )
                approved_application_ids.add(target_plan.application_id)
            prepared_approvals[operation_id] = (
                proposal,
                extraction,
                final_preview,
            )

        for row in rows:
            operation_id = row[1]
            decision = decision_by_id.get(operation_id)
            if decision is None or current_by_id[operation_id]["state"] != "pending_confirmation":
                continue
            if decision.action == "approve":
                proposal, extraction, final_preview = prepared_approvals[operation_id]
                if decision.edited_extraction is not None:
                    changed = conn.execute(
                        "UPDATE journal SET derivation_json = ? WHERE user_id = ? "
                        "AND id = ? AND kind = 'correction' AND operation_id = ? "
                        "AND state = 'awaiting_user' AND revision = 1",
                        (
                            _canonical_json(_pending_confirmation_derivation(
                                proposal,
                                final_preview,
                            )),
                            user_id,
                            row[0],
                            operation_id,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ReviewRecordOperationConflict(
                            "批量中的岗位编辑发生竞争，整批没有执行",
                        )
                try:
                    decided = _finalize_operation_in_transaction(
                        conn,
                        user_id,
                        proposal,
                        extraction,
                        frozen_target_plan=final_preview.target_plan,
                        target_plan_prevalidated=True,
                    )
                except IntegrityError as error:
                    if not _is_application_identity_collision(error):
                        raise
                    raise ReviewRecordOperationConflict(
                        "批量中的公司和岗位与现有岗位重复，整批没有执行",
                    ) from error
                expected_state = "completed"
            else:
                decided = _reject_review_record_operation_in_transaction(
                    conn,
                    user_id,
                    operation_id,
                )
                expected_state = "rejected"
            if decided["state"] != expected_state:
                raise ReviewRecordOperationConflict(
                    "批量中的至少一条复盘已变化，整批没有执行",
                )

        return [
            _dto(conn, user_id, _operation_row(conn, user_id, row[1]))
            for row in rows
            if row[1] in decision_by_id
        ]
