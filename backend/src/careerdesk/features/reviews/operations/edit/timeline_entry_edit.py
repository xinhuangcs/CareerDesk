"""Direct Timeline-page edit for one Review-owned timeline entry."""

from __future__ import annotations

from .....platform.database import now_iso, transaction
from .bundle import _canonical_json, _load_bundle, timeline_entry_snapshot_fingerprint
from .errors import (
    ReviewTimelineEntryEditOperationConflict,
    _EditTargetMissing,
    _UnsafeEditDependency,
)
from .projection import _apply_entry_projection_in_transaction


def edit_review_timeline_entry_from_timeline(
    db_path: str,
    user_id: str,
    application_id: int,
    timeline_entry_id: int,
    *,
    expected_revision: int,
    expected_fingerprint: str,
    step: str | None,
    occurred_date: str | None,
    outcome: str | None,
    summary: str | None,
) -> dict | None:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision 必须是非负整数")
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT source.id FROM timeline_entries entry JOIN journal source "
            "ON source.id = entry.journal_id AND source.user_id = entry.user_id "
            "WHERE entry.user_id = ? AND entry.application_id = ? AND entry.id = ? "
            "AND source.kind = 'review'",
            (user_id, application_id, timeline_entry_id),
        ).fetchone()
        if source is None:
            return None
        try:
            before = _load_bundle(conn, user_id, source[0])
        except _EditTargetMissing:
            return None
        except _UnsafeEditDependency as error:
            raise ReviewTimelineEntryEditOperationConflict(str(error)) from error
        if (
            before.target.application_id != application_id
            or before.target.timeline_entry_id != timeline_entry_id
        ):
            raise ReviewTimelineEntryEditOperationConflict("复盘目标与时间线选择不一致")
        if before.application.revision != expected_revision:
            raise ReviewTimelineEntryEditOperationConflict("岗位已在另一窗口修改，请刷新后重试")
        if timeline_entry_snapshot_fingerprint(before) != expected_fingerprint:
            raise ReviewTimelineEntryEditOperationConflict("这条历程已被另一窗口修改，请刷新后合并")
        final_values = {
            "step": step,
            "occurred_date": occurred_date,
            "outcome": outcome,
            "summary": summary,
        }
        current_values = {
            "step": before.entry.step,
            "occurred_date": before.entry.occurred_date,
            "outcome": before.entry.outcome,
            "summary": before.entry.summary,
        }
        changed_fields = {
            field for field, value in final_values.items()
            if current_values[field] != value
        }
        if not changed_fields:
            return {
                "id": timeline_entry_id,
                **current_values,
                "from_stage": before.entry.from_stage,
                "from_step": before.entry.from_step,
                "to_stage": before.entry.to_stage,
                "to_step": before.entry.to_step,
                "created_time": before.entry_row["created_time"],
                "source": "review",
                "snapshot_fingerprint": expected_fingerprint,
            }
        timestamp = now_iso()
        write = _apply_entry_projection_in_transaction(
            conn,
            user_id,
            before,
            values=final_values,
            changed_fields=changed_fields,
            timestamp=timestamp,
        )
        conn.execute(
            "INSERT INTO journal "
            "(user_id, kind, content, created_time, processed_time, extraction_json, "
            "derivation_json, state, parent_journal_id) "
            "VALUES (?, 'correction', ?, ?, ?, ?, ?, 'applied', ?)",
            (
                user_id,
                f"[时间线修正复盘历程] {before.target.company}·{before.target.position}",
                timestamp,
                timestamp,
                _canonical_json({
                    "operation_type": "review_timeline_entry_edit",
                    "application_id": application_id,
                    "timeline_entry_id": timeline_entry_id,
                    "before": current_values,
                    "after": final_values,
                }),
                _canonical_json({"operation": {"type": "review_timeline_entry_edit"}}),
                before.journal_id,
            ),
        )
        return {
            "id": timeline_entry_id,
            "step": write.entry.step,
            "occurred_date": write.entry.occurred_date,
            "outcome": write.entry.outcome,
            "summary": write.entry.summary,
            "from_stage": write.entry.from_stage,
            "from_step": write.entry.from_step,
            "to_stage": write.entry.to_stage,
            "to_step": write.entry.to_step,
            "created_time": before.entry_row["created_time"],
            "source": "review",
            "snapshot_fingerprint": timeline_entry_snapshot_fingerprint(write.after),
        }
