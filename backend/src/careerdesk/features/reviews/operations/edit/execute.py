"""Execute a trusted, immediately reversible Review timeline-entry edit."""

from __future__ import annotations

from uuid import UUID

from .....platform.database import now_iso, transaction
from ..edit_models import (
    REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION,
    ReviewTimelineEntryEditApplyResult,
    ReviewTimelineEntryEditEffect,
    ReviewTimelineEntryEditProposal,
    ReviewTimelineEntryEditReceipt,
)
from .errors import (
    ReviewTimelineEntryEditOperationConflict,
    ReviewTimelineEntryEditOperationNotFound,
)
from .projection import _apply_entry_projection_in_transaction
from .read import _dto, _operation_row
from .validate import (
    MAX_OPERATIONS_PER_TURN,
    _canonical_json,
    _canonical_uuid,
    _command,
    _locate_target,
    _proposal_digest,
    _request_digest,
    _review_edit_operation_count,
)


def execute_review_timeline_entry_edit_operation(
    db_path: str,
    user_id: str,
    *,
    operation_id: str | UUID,
    client_turn_id: str | UUID,
    company: str | None,
    position: str | None,
    changes: dict,
) -> dict:
    canonical_operation = _canonical_uuid(operation_id, label="operation_id")
    canonical_turn = _canonical_uuid(client_turn_id, label="client_turn_id")
    command = _command(company, position, changes)
    request_digest = _request_digest(canonical_turn, command)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _operation_row(conn, user_id, canonical_operation)
        if existing is not None:
            dto = _dto(conn, user_id, existing)
            if (
                dto["client_turn_id"] != canonical_turn
                or dto["request_digest"] != request_digest
            ):
                raise ReviewTimelineEntryEditOperationConflict(
                    "复盘历程修正 operation 与已有命令冲突",
                )
            return dto
        if conn.execute(
            "SELECT 1 FROM journal WHERE operation_id = ? LIMIT 1",
            (canonical_operation,),
        ).fetchone() is not None:
            raise ReviewTimelineEntryEditOperationNotFound(
                "复盘历程修正操作不存在",
            )
        if (
            _review_edit_operation_count(conn, user_id, canonical_turn)
            >= MAX_OPERATIONS_PER_TURN
        ):
            raise ReviewTimelineEntryEditOperationConflict(
                "单轮复盘历程修正已达安全上限",
            )

        located = _locate_target(conn, user_id, command)
        if located["status"] != "ok":
            return located
        bundle = located["bundle"]
        requested = command.changes.model_dump(
            mode="json",
            include=command.changes.model_fields_set,
        )
        before_values = {
            "step": bundle.entry.step,
            "occurred_date": bundle.entry.occurred_date,
            "outcome": bundle.entry.outcome,
            "summary": bundle.entry.summary,
        }
        changed_fields = {
            field for field, value in requested.items()
            if before_values[field] != value
        }
        if not changed_fields:
            return {
                "status": "no_change",
                "company": bundle.target.company,
                "position": bundle.target.position,
            }
        timestamp = now_iso()
        write = _apply_entry_projection_in_transaction(
            conn,
            user_id,
            bundle,
            values=requested,
            changed_fields=changed_fields,
            timestamp=timestamp,
        )
        effect = ReviewTimelineEntryEditEffect(
            changed_fields=[
                field for field in ("step", "occurred_date", "outcome", "summary")
                if field in changed_fields
            ],
            occurrences=list(write.occurrence_effects),
            status_logs=list(write.status_effects),
            application_before=bundle.application,
            application_final=write.application,
            before_dependency_fingerprint=bundle.fingerprint,
            final_dependency_fingerprint=write.after.fingerprint,
            questions_untouched=True,
            knowledge_untouched=True,
        )
        proposal = ReviewTimelineEntryEditProposal(
            operation_type="review_timeline_entry_edit",
            contract_version=REVIEW_TIMELINE_ENTRY_EDIT_CONTRACT_VERSION,
            client_turn_id=canonical_turn,
            request_digest=request_digest,
            target=bundle.target,
            before=bundle.entry,
            final=write.entry,
            effect=effect,
        )
        result = ReviewTimelineEntryEditReceipt(
            apply=ReviewTimelineEntryEditApplyResult(
                status="ok",
                journal_id=bundle.journal_id,
                timeline_entry_id=bundle.target.timeline_entry_id,
                application_id=bundle.target.application_id,
                target_revision=write.entry.journal_revision,
                application_revision=write.application.revision,
                timeline_entries_updated=1,
                occurrences_updated=len(write.occurrence_effects),
                status_logs_updated=len(write.status_effects),
                application_updated=True,
            ),
        )
        derivation = {"operation": {
            "type": "review_timeline_entry_edit",
            "action": "complete",
            "client_turn_id": canonical_turn,
            "proposal_digest": _proposal_digest(proposal),
            "result": result.model_dump(mode="json"),
        }}
        conn.execute(
            "INSERT INTO journal "
            "(user_id, kind, content, created_time, processed_time, extraction_json, "
            "derivation_json, state, parent_journal_id, operation_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, ?, 'applied', ?, ?)",
            (
                user_id,
                f"[修正复盘历程] {bundle.target.company}·{bundle.target.position}",
                timestamp,
                timestamp,
                _canonical_json(proposal.model_dump(mode="json")),
                _canonical_json(derivation),
                bundle.journal_id,
                canonical_operation,
            ),
        )
        row = _operation_row(conn, user_id, canonical_operation)
        if row is None:
            raise ReviewTimelineEntryEditOperationNotFound("复盘历程修正操作未发布")
        return _dto(conn, user_id, row)
