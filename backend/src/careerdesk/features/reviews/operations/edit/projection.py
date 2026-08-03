"""Atomically edit one Review-owned timeline entry and all owned projections."""

from __future__ import annotations

from dataclasses import dataclass
import json
from sqlite3 import Connection

from pydantic import ValidationError

from ...ai_models import ReviewExtraction, ReviewHistoryFact
from ..edit_models import (
    ReviewTimelineEntryApplicationProjection,
    ReviewTimelineEntryOccurrenceEffect,
    ReviewTimelineEntryProjection,
    ReviewTimelineEntryStatusLogEffect,
)
from ..record_models import ReviewRecordDerivation
from .bundle import _Bundle, _canonical_json, _load_bundle
from .errors import ReviewTimelineEntryEditOperationConflict


@dataclass(frozen=True, slots=True)
class _ProjectionWriteResult:
    after: _Bundle
    entry: ReviewTimelineEntryProjection
    extraction: ReviewExtraction
    application: ReviewTimelineEntryApplicationProjection
    occurrence_effects: tuple[ReviewTimelineEntryOccurrenceEffect, ...]
    status_effects: tuple[ReviewTimelineEntryStatusLogEffect, ...]


def _apply_entry_projection_in_transaction(
    conn: Connection,
    user_id: str,
    bundle: _Bundle,
    *,
    values: dict[str, str | None],
    changed_fields: set[str],
    timestamp: str,
) -> _ProjectionWriteResult:
    current = {
        "step": bundle.entry.step,
        "occurred_date": bundle.entry.occurred_date,
        "outcome": bundle.entry.outcome,
        "summary": bundle.entry.summary,
    }
    final = {**current, **values}
    state_source = (
        bundle.application.current_state_entry_id == bundle.target.timeline_entry_id
    )
    step_drives_current = (
        state_source
        and bundle.entry.from_step != bundle.entry.to_step
        and bundle.entry.to_step == bundle.entry.step
        and bundle.application.current_step == bundle.entry.to_step
    )
    final_to_step = final["step"] if step_drives_current else bundle.entry.to_step
    final_current_step = (
        final["step"] if step_drives_current else bundle.application.current_step
    )
    try:
        final_entry = ReviewTimelineEntryProjection(
            **final,
            from_stage=bundle.entry.from_stage,
            from_step=bundle.entry.from_step,
            to_stage=bundle.entry.to_stage,
            to_step=final_to_step,
            journal_revision=bundle.journal_revision + 1,
        )
        extraction_values = bundle.extraction.model_dump(mode="json")
        history = dict(extraction_values.get("history") or {})
        history.update({
            "step": final["step"],
            "date": final["occurred_date"],
            "outcome": final["outcome"],
            "summary": final["summary"],
        })
        extraction_values["history"] = ReviewHistoryFact.model_validate(history).model_dump(
            mode="json",
        )
        if step_drives_current and extraction_values.get("projected_state") is not None:
            extraction_values["projected_state"]["current_step"] = final_current_step
        final_extraction = ReviewExtraction.model_validate(extraction_values)
        final_application = ReviewTimelineEntryApplicationProjection(
            stage=bundle.application.stage,
            current_step=final_current_step,
            current_state_entry_id=bundle.application.current_state_entry_id,
            revision=bundle.application.revision + 1,
        )
        raw_derivation = json.loads(bundle.derivation_raw)
        derivation_values = ReviewRecordDerivation.model_validate({
            **raw_derivation,
            "revision": bundle.journal_revision,
        }).model_dump(mode="json")
        if step_drives_current:
            derivation_values["application_after"]["current_step"] = final_current_step
        derivation_values["application_after"]["revision"] = final_application.revision
        derivation_values["revision"] = bundle.journal_revision + 1
        final_derivation = ReviewRecordDerivation.model_validate(derivation_values)
    except ValidationError as error:
        raise ReviewTimelineEntryEditOperationConflict(
            "复盘历程修改后不再构成有效事实",
        ) from error

    occurrence_effects: list[ReviewTimelineEntryOccurrenceEffect] = []
    for occurrence in bundle.occurrences:
        next_step = final["step"] if "step" in changed_fields else occurrence["source_step"]
        next_date = (
            final["occurred_date"]
            if "occurred_date" in changed_fields
            else occurrence["asked_date"]
        )
        if (next_step, next_date) == (
            occurrence["source_step"], occurrence["asked_date"],
        ):
            continue
        occurrence_effects.append(ReviewTimelineEntryOccurrenceEffect(
            question_id=occurrence["question_id"],
            application_id=occurrence["application_id"],
            company=occurrence["company"],
            before_source_step=occurrence["source_step"],
            after_source_step=next_step,
            before_asked_date=occurrence["asked_date"],
            after_asked_date=next_date,
        ))

    status_effects: list[ReviewTimelineEntryStatusLogEffect] = []
    if "occurred_date" in changed_fields and bundle.status_logs:
        if final["occurred_date"] is None:
            raise ReviewTimelineEntryEditOperationConflict(
                "带状态日志的复盘不能清空发生日期",
            )
        for status_log in bundle.status_logs:
            if status_log["log_date"] == final["occurred_date"]:
                continue
            status_effects.append(ReviewTimelineEntryStatusLogEffect(
                id=status_log["id"],
                created_time=status_log["created_time"],
                before_log_date=status_log["log_date"],
                after_log_date=final["occurred_date"],
            ))

    changes_before = conn.total_changes
    changed = conn.execute(
        "UPDATE journal SET processed_time = ?, extraction_json = ?, derivation_json = ?, "
        "revision = revision + 1 "
        "WHERE user_id = ? AND id = ? AND kind = 'review' AND state = 'applied' "
        "AND revision = ? AND created_time = ? AND extraction_json = ? AND derivation_json = ?",
        (
            timestamp,
            _canonical_json(final_extraction.model_dump(mode="json")),
            _canonical_json(final_derivation.model_dump(mode="json", exclude={"revision"})),
            user_id,
            bundle.journal_id,
            bundle.journal_revision,
            bundle.journal_created_time,
            bundle.extraction_raw,
            bundle.derivation_raw,
        ),
    ).rowcount
    if changed != 1:
        raise ReviewTimelineEntryEditOperationConflict("目标复盘已被其它窗口修改")

    entry = bundle.entry_row
    changed = conn.execute(
        "UPDATE timeline_entries SET step = ?, occurred_date = ?, outcome = ?, summary = ?, "
        "to_step = ? WHERE id = ? AND user_id = ? AND application_id = ? AND journal_id = ? "
        "AND created_time = ? AND step IS ? AND occurred_date IS ? AND outcome IS ? "
        "AND summary IS ? AND to_step IS ?",
        (
            final["step"],
            final["occurred_date"],
            final["outcome"],
            final["summary"],
            final_to_step,
            entry["id"],
            user_id,
            entry["application_id"],
            bundle.journal_id,
            entry["created_time"],
            entry["step"],
            entry["occurred_date"],
            entry["outcome"],
            entry["summary"],
            entry["to_step"],
        ),
    ).rowcount
    if changed != 1:
        raise ReviewTimelineEntryEditOperationConflict("目标历程已被其它窗口修改")

    for effect in occurrence_effects:
        changed = conn.execute(
            "UPDATE review_question_occurrences SET source_step = ?, asked_date = ? "
            "WHERE user_id = ? AND journal_id = ? AND question_id = ? "
            "AND application_id = ? AND company = ? AND source_step IS ? AND asked_date IS ?",
            (
                effect.after_source_step,
                effect.after_asked_date,
                user_id,
                bundle.journal_id,
                effect.question_id,
                effect.application_id,
                effect.company,
                effect.before_source_step,
                effect.before_asked_date,
            ),
        ).rowcount
        if changed != 1:
            raise ReviewTimelineEntryEditOperationConflict("真题出处已被其它窗口修改")

    for effect in status_effects:
        changed = conn.execute(
            "UPDATE status_log SET log_date = ? WHERE user_id = ? AND id = ? "
            "AND journal_id = ? AND created_time = ? AND log_date = ?",
            (
                effect.after_log_date,
                user_id,
                effect.id,
                bundle.journal_id,
                effect.created_time,
                effect.before_log_date,
            ),
        ).rowcount
        if changed != 1:
            raise ReviewTimelineEntryEditOperationConflict("状态日志已被其它窗口修改")

    application = bundle.application_row
    changed = conn.execute(
        "UPDATE applications SET current_step = ?, revision = revision + 1, updated_time = ? "
        "WHERE user_id = ? AND id = ? AND created_time = ? AND stage = ? "
        "AND current_step IS ? AND current_state_entry_id IS ? AND revision = ?",
        (
            final_current_step,
            timestamp,
            user_id,
            application["id"],
            application["created_time"],
            application["stage"],
            application["current_step"],
            application["current_state_entry_id"],
            application["revision"],
        ),
    ).rowcount
    if changed != 1:
        raise ReviewTimelineEntryEditOperationConflict("岗位投影已被其它窗口修改")

    expected_changes = 3 + len(occurrence_effects) + len(status_effects)
    if conn.total_changes - changes_before != expected_changes:
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修正产生未展示的额外写入")
    after = _load_bundle(conn, user_id, bundle.journal_id)
    if (
        after.entry != final_entry
        or after.extraction != final_extraction
        or after.application != final_application
    ):
        raise ReviewTimelineEntryEditOperationConflict("复盘历程修改后的投影不一致")
    return _ProjectionWriteResult(
        after=after,
        entry=final_entry,
        extraction=final_extraction,
        application=final_application,
        occurrence_effects=tuple(occurrence_effects),
        status_effects=tuple(status_effects),
    )
