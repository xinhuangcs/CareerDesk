"""Review projection/undo across state, history, questions, and status atomically."""

from __future__ import annotations

import json
from sqlite3 import Connection

from ...platform.database import (
    application_identity_key,
    normalize_application_identity_part,
    now_iso,
    squash_whitespace,
    transaction,
)
from ..applications.public import ApplicationNextAction, apply_application_progress_in_transaction
from ..companies.public import ensure_company_in_transaction
from ..journal import public as journal
from ..knowledge import public as knowledge
from .ai_models import ReviewExtraction

UNKNOWN_POSITION = "未注明岗位"


class ReviewConflict(RuntimeError):
    """Review identity/state/revision or application projection has changed."""


def _resolve_application(
    conn: Connection,
    user_id: str,
    company: str,
    position: str | None,
    company_id: int,
    *,
    company_fallback: bool = True,
) -> tuple[int, bool]:
    if position:
        identity_key = application_identity_key(company, position)
        row = conn.execute(
            "SELECT id FROM applications WHERE user_id = ? "
            "AND company_key = ? AND position_key = ?",
            (user_id, *identity_key),
        ).fetchone()
        if row is not None:
            return row[0], False
    company_key = normalize_application_identity_part(company)
    rows = conn.execute(
        "SELECT id FROM applications WHERE user_id = ? AND company_key = ? "
        "ORDER BY id LIMIT 2",
        (user_id, company_key),
    ).fetchall()
    if len(rows) == 1 and company_fallback:
        return rows[0][0], False
    timestamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO applications "
        "(user_id, company, company_id, position, created_time, updated_time) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, company, company_id, position or UNKNOWN_POSITION, timestamp, timestamp),
    )
    return cursor.lastrowid, True


def _upsert_real_question(
    conn: Connection,
    user_id: str,
    question: dict,
    *,
    application_id: int,
    company: str,
    source_step: str | None,
    asked_date: str | None,
    journal_id: int,
) -> int:
    row = conn.execute(
        "SELECT id FROM questions WHERE user_id = ? AND source = 'real' AND text = ?",
        (user_id, question["text"]),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE questions SET status = 'active', updated_time = ? "
            "WHERE user_id = ? AND id = ?",
            (now_iso(), user_id, row[0]),
        )
        return row[0]
    timestamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO questions "
        "(user_id, text, source, company, source_step, asked_date, application_id, "
        "journal_id, created_time, updated_time) "
        "VALUES (?, ?, 'real', ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            question["text"],
            company,
            source_step,
            asked_date,
            application_id,
            journal_id,
            timestamp,
            timestamp,
        ),
    )
    return cursor.lastrowid


def _question_has_active_review(
    conn: Connection,
    user_id: str,
    question_id: int,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM review_question_occurrences occurrence "
        "JOIN journal source ON source.user_id = occurrence.user_id "
        "AND source.id = occurrence.journal_id "
        "WHERE occurrence.user_id = ? AND occurrence.question_id = ? "
        "AND source.kind = 'review' AND source.state = 'applied' LIMIT 1",
        (user_id, question_id),
    ).fetchone()
    return row is not None


def _sync_question_occurrences(
    conn: Connection,
    user_id: str,
    journal_id: int,
    question_ids: list[int],
    *,
    application_id: int,
    company: str,
    source_step: str | None,
    asked_date: str | None,
) -> int:
    old_ids = {
        question_id
        for (question_id,) in conn.execute(
            "SELECT question_id FROM review_question_occurrences "
            "WHERE user_id = ? AND journal_id = ?",
            (user_id, journal_id),
        ).fetchall()
    }
    conn.execute(
        "DELETE FROM review_question_occurrences WHERE user_id = ? AND journal_id = ?",
        (user_id, journal_id),
    )
    for question_id in question_ids:
        conn.execute(
            "INSERT INTO review_question_occurrences "
            "(user_id, journal_id, question_id, application_id, company, source_step, asked_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                journal_id,
                question_id,
                application_id,
                company,
                source_step,
                asked_date,
            ),
        )
    archived = 0
    for question_id in old_ids - set(question_ids):
        if not _question_has_active_review(conn, user_id, question_id):
            archived += conn.execute(
                "UPDATE questions SET status = 'archived', updated_time = ? "
                "WHERE user_id = ? AND id = ? AND source = 'real' AND status = 'active'",
                (now_iso(), user_id, question_id),
            ).rowcount
    return archived


def _derive_review_in_transaction(
    conn: Connection,
    user_id: str,
    journal_id: int,
    extraction: dict,
    *,
    replay: bool,
    expected_state: str,
    expected_revision: int,
    reuse_current_application: bool = False,
    preserve_application_projection: bool = False,
    application_stage_transition: tuple[str, str] | None = None,
    frozen_application_id: int | None = None,
    force_exact_new_application: bool = False,
) -> dict:
    claim_revision = journal.claim_review_in_transaction(
        conn,
        user_id,
        journal_id,
        expected_state=expected_state,
        expected_revision=expected_revision,
    )
    if claim_revision is None:
        raise ReviewConflict("review state or revision changed")

    model = ReviewExtraction.model_validate(extraction)
    extraction = model.model_dump(mode="json")
    if reuse_current_application and frozen_application_id is not None:
        raise ReviewConflict("review application target cannot use two frozen modes")
    if frozen_application_id is not None:
        current = conn.execute(
            "SELECT id, company, position FROM applications WHERE user_id = ? AND id = ?",
            (user_id, frozen_application_id),
        ).fetchone()
        if current is None or squash_whitespace(current[1]) != squash_whitespace(
            extraction["company"],
        ):
            raise ReviewConflict("frozen review application no longer matches")
        application_id, company, position = current
        application_created = False
    elif reuse_current_application:
        current = conn.execute(
            "SELECT application.id, application.company, application.position "
            "FROM timeline_entries entry JOIN applications application "
            "ON application.user_id = entry.user_id AND application.id = entry.application_id "
            "WHERE entry.user_id = ? AND entry.journal_id = ? "
            "ORDER BY entry.id DESC LIMIT 1",
            (user_id, journal_id),
        ).fetchone()
        if current is None:
            raise ReviewConflict("review no longer has a current application")
        application_id, company, position = current
        extraction["company"] = company
        extraction["position"] = position
        application_created = False
    else:
        company = extraction["company"]
        company_id = ensure_company_in_transaction(conn, user_id, company)
        position = extraction.get("position")
        application_id, application_created = _resolve_application(
            conn,
            user_id,
            company,
            position,
            company_id,
            company_fallback=(position is None and not force_exact_new_application),
        )

    application = conn.execute(
        "SELECT stage, current_step, current_state_entry_id, channel, applied_date, revision, "
        "next_stage, next_step, next_date, next_time, next_note "
        "FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if application is None:
        raise ReviewConflict("application ownership changed")
    (
        current_stage,
        current_step,
        current_state_entry_id,
        current_channel,
        current_applied_date,
        current_revision,
        next_stage,
        next_step,
        next_date,
        next_time,
        next_note,
    ) = application
    current_next_action = (
        None
        if next_step is None
        else ApplicationNextAction(
            stage=next_stage,
            step=next_step,
            date=next_date,
            time=next_time,
            note=next_note,
        )
    )
    if model.clear_next_action and current_next_action is None:
        raise ReviewConflict("当前没有可清除的下一步安排")
    existing_entries = conn.execute(
        "SELECT id FROM timeline_entries WHERE user_id = ? AND journal_id = ?",
        (user_id, journal_id),
    ).fetchall()
    existing_ids = {row[0] for row in existing_entries}
    if current_state_entry_id in existing_ids:
        conn.execute(
            "UPDATE applications SET current_state_entry_id = NULL "
            "WHERE user_id = ? AND id = ? AND current_state_entry_id = ?",
            (user_id, application_id, current_state_entry_id),
        )
        current_state_entry_id = None
    conn.execute(
        "DELETE FROM timeline_entries WHERE user_id = ? AND journal_id = ?",
        (user_id, journal_id),
    )
    conn.execute(
        "DELETE FROM status_log WHERE user_id = ? AND journal_id = ?",
        (user_id, journal_id),
    )

    projected = model.projected_state
    projected_stage = current_stage
    projected_step = current_step
    update_current_state = not preserve_application_projection and projected is not None
    if update_current_state:
        projected_stage = projected.stage or current_stage
        projected_step = projected.current_step or current_step
    if preserve_application_projection and application_stage_transition is not None:
        expected_stage, replacement_stage = application_stage_transition
        if current_stage != expected_stage:
            raise ReviewConflict("application stage changed during review correction")
        projected_stage = replacement_stage

    if projected_stage in {"withdrawn", "rejected"} and model.next_action is not None:
        raise ReviewConflict("已挂或不再跟进的岗位不能同时安排下一步")
    if (
        model.history is None
        and not model.clear_next_action
        and model.next_action is None
        and (projected_stage, projected_step) == (current_stage, current_step)
    ):
        raise ReviewConflict("复盘没有可记录的新事实或状态变化")

    history = model.history
    # A future plan is a fact about scheduling, not a completed current step.
    # Keep its audit trail in the summary while leaving the factual step empty.
    entry_step = history.step if history is not None else None
    entry_date = history.date if history is not None else None
    entry_outcome = history.outcome if history is not None else None
    entry_summary = history.summary if history is not None else (
        f"已安排下一步：{model.next_action.step}"
        if model.next_action is not None
        else "已清除下一步安排" if model.clear_next_action else None
    )
    timestamp = now_iso()
    progress = apply_application_progress_in_transaction(
        conn,
        user_id,
        application_id,
        expected_revision=current_revision,
        step=entry_step,
        occurred_date=entry_date,
        outcome=entry_outcome,
        summary=entry_summary,
        update_current_state=update_current_state or application_stage_transition is not None,
        target_stage=projected_stage if (update_current_state or application_stage_transition) else None,
        target_step=projected_step if (update_current_state or application_stage_transition) else None,
        replace_next_action=(
            not preserve_application_projection
            and (
                model.clear_next_action
                or model.next_action is not None
                or projected_stage in {"withdrawn", "rejected"}
            )
        ),
        next_action=(
            model.next_action.model_dump(mode="json")
            if model.next_action is not None else None
        ),
        use_fact_step_for_current=False,
        source="review",
        journal_id=journal_id,
        timestamp=timestamp,
    )
    if progress is None:
        raise ReviewConflict("application ownership changed")
    timeline_entry_ids = [progress["entry_id"]]

    source_step = history.step if history is not None else (
        projected.current_step if projected is not None else None
    )
    question_ids: list[int] = []
    knowledge_point_ids: list[int] = []
    for question in extraction.get("questions", []):
        question_id = _upsert_real_question(
            conn,
            user_id,
            question,
            application_id=application_id,
            company=company,
            source_step=source_step,
            asked_date=entry_date,
            journal_id=journal_id,
        )
        if question_id not in question_ids:
            question_ids.append(question_id)
        for name in question.get("knowledge_points", []):
            knowledge_point_id = knowledge.touch_knowledge_point_in_transaction(
                conn,
                user_id,
                name,
                stuck=question.get("stuck", False),
                replay=replay,
            )
            knowledge_point_ids.append(knowledge_point_id)
            knowledge.link_question_knowledge_in_transaction(
                conn,
                user_id,
                question_id,
                knowledge_point_id,
            )
    _sync_question_occurrences(
        conn,
        user_id,
        journal_id,
        question_ids,
        application_id=application_id,
        company=company,
        source_step=source_step,
        asked_date=entry_date,
    )

    status_log_ids: list[int] = []
    if entry_date and (model.mood or model.factors or model.time_of_day):
        cursor = conn.execute(
            "INSERT INTO status_log "
            "(user_id, log_date, time_of_day, mood, factors_json, journal_id, created_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                entry_date,
                model.time_of_day,
                model.mood,
                json.dumps(model.factors, ensure_ascii=False),
                journal_id,
                timestamp,
            ),
        )
        status_log_ids.append(cursor.lastrowid)

    projected_applied_date = current_applied_date
    if (
        current_stage != "applied"
        and projected_stage == "applied"
        and projected_applied_date is None
    ):
        projected_applied_date = entry_date
    changed = conn.execute(
        "UPDATE applications SET channel = ?, applied_date = ?, updated_time = ? "
        "WHERE user_id = ? AND id = ? AND revision = ?",
        (
            model.channel or current_channel,
            projected_applied_date,
            timestamp,
            user_id,
            application_id,
            progress["revision"],
        ),
    ).rowcount
    if changed != 1:
        raise ReviewConflict("application metadata changed during review")

    derivation = {
        "application_id": application_id,
        "application_created": application_created,
        "timeline_entry_ids": timeline_entry_ids,
        "question_ids": question_ids,
        "knowledge_point_ids": sorted(set(knowledge_point_ids)),
        "status_log_ids": status_log_ids,
        "application_before": {
            "stage": progress["before"]["stage"],
            "current_step": progress["before"]["current_step"],
            "current_state_entry_id": progress["before"]["current_state_entry_id"],
            "next_action": progress["before"]["next_action"],
            "paused_from_stage": progress["before"]["paused_from_stage"],
            "pause_reason": progress["before"]["pause_reason"],
            "channel": current_channel,
            "applied_date": current_applied_date,
            "revision": progress["before"]["revision"],
        },
        "application_after": {
            "stage": progress["after"]["stage"],
            "current_step": progress["after"]["current_step"],
            "current_state_entry_id": progress["after"]["current_state_entry_id"],
            "next_action": progress["after"]["next_action"],
            "paused_from_stage": progress["after"]["paused_from_stage"],
            "pause_reason": progress["after"]["pause_reason"],
            "channel": model.channel or current_channel,
            "applied_date": projected_applied_date,
            "revision": progress["after"]["revision"],
        },
    }
    derivation["revision"] = journal.finish_review_in_transaction(
        conn,
        user_id,
        journal_id,
        claim_revision=claim_revision,
        extraction=extraction,
        derivation=derivation,
    )
    return derivation


def derive_review(
    db_path: str,
    user_id: str,
    journal_id: int,
    extraction: dict,
    *,
    replay: bool = False,
    expected_state: str = "pending",
    expected_revision: int = 0,
    reuse_current_application: bool = False,
    preserve_application_projection: bool = False,
    application_stage_transition: tuple[str, str] | None = None,
) -> dict:
    """Preserve the repository-level transactional entry point for existing callers."""
    canonical = ReviewExtraction.model_validate(extraction).model_dump(mode="json")
    with transaction(db_path) as conn:
        return _derive_review_in_transaction(
            conn,
            user_id,
            journal_id,
            canonical,
            replay=replay,
            expected_state=expected_state,
            expected_revision=expected_revision,
            reuse_current_application=reuse_current_application,
            preserve_application_projection=preserve_application_projection,
            application_stage_transition=application_stage_transition,
        )


def _undo_review_in_transaction(
    conn: Connection,
    user_id: str,
    journal_id: int,
    *,
    expected_revision: int,
    derivation: dict,
    expected_application_stage: str | None = None,
    replacement_application_stage: str | None = None,
    delete_application: bool = False,
) -> dict:
    revision = journal.void_review_in_transaction(
        conn,
        user_id,
        journal_id,
        expected_revision=expected_revision,
        derivation=derivation,
    )
    if revision is None:
        raise ReviewConflict("review state or revision changed")
    entry = conn.execute(
        "SELECT entry.id, entry.application_id, entry.from_stage, entry.from_step "
        "FROM timeline_entries entry JOIN applications application "
        "ON application.id = entry.application_id AND application.user_id = entry.user_id "
        "WHERE entry.user_id = ? AND entry.journal_id = ? ORDER BY entry.id DESC LIMIT 1",
        (user_id, journal_id),
    ).fetchone()
    application_id = entry[1] if entry is not None else None
    if delete_application and application_id is None:
        raise ReviewConflict("review-created application no longer exists")
    if delete_application:
        from ..applications.public import remove_review_created_application_in_transaction

        try:
            delete_result = remove_review_created_application_in_transaction(
                conn,
                user_id,
                application_id,
                source_journal_id=journal_id,
            )
        except RuntimeError as error:
            raise ReviewConflict(str(error)) from error
        entries_removed = delete_result.get("timeline_entries_removed", 0)
    else:
        if entry is not None:
            current = conn.execute(
                "SELECT stage, current_state_entry_id, revision FROM applications "
                "WHERE user_id = ? AND id = ?",
                (user_id, application_id),
            ).fetchone()
            if current is None:
                raise ReviewConflict("review application no longer exists")
            if current[1] == entry[0]:
                expected_stage = expected_application_stage or current[0]
                replacement_stage = replacement_application_stage or entry[2]
                changed = conn.execute(
                    "UPDATE applications SET stage = ?, current_step = ?, "
                    "current_state_entry_id = NULL, revision = revision + 1, updated_time = ? "
                    "WHERE user_id = ? AND id = ? AND stage = ? AND revision = ?",
                    (
                        replacement_stage,
                        entry[3],
                        now_iso(),
                        user_id,
                        application_id,
                        expected_stage,
                        current[2],
                    ),
                ).rowcount
                if changed != 1:
                    raise ReviewConflict("application stage changed during review undo")
        entries_removed = conn.execute(
            "DELETE FROM timeline_entries WHERE user_id = ? AND journal_id = ?",
            (user_id, journal_id),
        ).rowcount
    status_logs_removed = conn.execute(
        "DELETE FROM status_log WHERE user_id = ? AND journal_id = ?",
        (user_id, journal_id),
    ).rowcount
    question_ids = [
        question_id
        for (question_id,) in conn.execute(
            "SELECT question_id FROM review_question_occurrences "
            "WHERE user_id = ? AND journal_id = ?",
            (user_id, journal_id),
        ).fetchall()
    ]
    archived = 0
    for question_id in question_ids:
        if not _question_has_active_review(conn, user_id, question_id):
            archived += conn.execute(
                "UPDATE questions SET status = 'archived', updated_time = ? "
                "WHERE user_id = ? AND id = ? AND source = 'real' AND status = 'active'",
                (now_iso(), user_id, question_id),
            ).rowcount
    return {
        "status": "ok",
        "application_id": None if delete_application else application_id,
        "application_stage": replacement_application_stage,
        "target_revision": revision,
        "removed": {
            "timeline_entries": entries_removed,
            "status_logs": status_logs_removed,
            "questions_archived": archived,
        },
    }


def reconcile_metadata_in_transaction(conn: Connection, user_id: str) -> None:
    """Canonical projection has one source of truth, so maintenance has nothing to replay."""
    del conn, user_id
