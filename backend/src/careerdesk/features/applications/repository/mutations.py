"""Transactional primitives shared by application operations."""

from sqlite3 import Connection

from ....platform.database import (
    application_identity_key,
    normalize_application_identity_part,
    now_iso,
    read_connection,
)
from .prep import _invalidated_semantic_prep_json
from .shared import ACTIVE_STAGES, CLOSED_STAGES, STAGE_LABELS, validate_iso_date


def resolve_application_by_name(
    db_path: str,
    user_id: str,
    company: str,
    position: str | None = None,
) -> dict:
    company_key = normalize_application_identity_part(company)
    if not company_key:
        return {"status": "not_found"}
    with read_connection(db_path) as conn:
        if position:
            try:
                exact_key = application_identity_key(company, position)
            except ValueError:
                return {"status": "not_found"}
            row = conn.execute(
                "SELECT id, company, position, revision, application_note "
                "FROM applications WHERE user_id = ? "
                "AND company_key = ? AND position_key = ?",
                (user_id, *exact_key),
            ).fetchone()
            if row is not None:
                return {
                    "status": "ok", "id": row[0], "company": row[1],
                    "position": row[2], "revision": row[3], "application_note": row[4],
                }
            return {"status": "not_found"}
        rows = conn.execute(
            "SELECT id, company, position, revision, application_note "
            "FROM applications WHERE user_id = ? AND company_key = ?",
            (user_id, company_key),
        ).fetchall()
    if len(rows) == 1:
        return {
            "status": "ok", "id": rows[0][0], "company": rows[0][1],
            "position": rows[0][2], "revision": rows[0][3],
            "application_note": rows[0][4],
        }
    if rows:
        return {"status": "ambiguous", "options": [row[2] for row in rows]}
    return {"status": "not_found"}


def _delete_application_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
) -> dict:
    if not conn.in_transaction:
        raise RuntimeError("岗位删除 executor 必须运行在显式写事务内")
    exists = conn.execute(
        "SELECT 1 FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()
    if exists is None:
        return {"status": "missing", "application_id": application_id}

    # applications.current_state_entry_id and timeline_entries.application_id form
    # a deliberate projection/history cycle; detach the projection edge first.
    conn.execute(
        "UPDATE applications SET current_state_entry_id = NULL "
        "WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    )
    timeline_entries_removed = conn.execute(
        "DELETE FROM timeline_entries WHERE user_id = ? AND application_id = ?",
        (user_id, application_id),
    ).rowcount
    questions_detached = conn.execute(
        "UPDATE questions SET application_id = NULL WHERE user_id = ? AND application_id = ?",
        (user_id, application_id),
    ).rowcount
    occurrences_detached = conn.execute(
        "UPDATE review_question_occurrences SET application_id = NULL "
        "WHERE user_id = ? AND application_id = ?",
        (user_id, application_id),
    ).rowcount
    resumes_detached = conn.execute(
        "UPDATE resumes SET application_id = NULL, binding = 'family' "
        "WHERE user_id = ? AND application_id = ?",
        (user_id, application_id),
    ).rowcount
    deleted = conn.execute(
        "DELETE FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).rowcount
    if deleted != 1:
        raise RuntimeError("岗位删除没有精确命中冻结目标")
    return {
        "status": "ok",
        "application_id": application_id,
        "timeline_entries_removed": timeline_entries_removed,
        "questions_detached": questions_detached,
        "question_occurrences_detached": occurrences_detached,
        "resumes_detached": resumes_detached,
    }


def remove_review_created_application_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
    *,
    source_journal_id: int,
) -> dict:
    if not conn.in_transaction:
        raise RuntimeError("Review-created 岗位删除必须运行在显式写事务内")
    foreign_entry = conn.execute(
        "SELECT 1 FROM timeline_entries WHERE user_id = ? AND application_id = ? "
        "AND journal_id IS NOT ? LIMIT 1",
        (user_id, application_id, source_journal_id),
    ).fetchone()
    if foreign_entry is not None:
        raise RuntimeError("岗位已有其它历程，不能随单条复盘一并删除")
    return _delete_application_in_transaction(conn, user_id, application_id)


def _merge_applications_in_transaction(
    conn: Connection,
    user_id: str,
    source_id: int,
    destination_id: int,
    *,
    final_projection: dict,
    destination_company: str,
) -> dict:
    if not conn.in_transaction:
        raise RuntimeError("岗位合并 executor 必须运行在显式写事务内")
    if source_id == destination_id:
        raise RuntimeError("岗位合并源与目标不能相同")
    rows = conn.execute(
        "SELECT id, company FROM applications WHERE user_id = ? AND id IN (?, ?) ORDER BY id",
        (user_id, source_id, destination_id),
    ).fetchall()
    identities = {row[0]: row[1] for row in rows}
    if set(identities) != {source_id, destination_id}:
        raise RuntimeError("岗位合并没有精确命中冻结双边")
    if identities[destination_id] != destination_company:
        raise RuntimeError("岗位合并目标公司已变化")
    required_fields = {
        "department", "channel", "jd_text", "jd_parsed_json", "stage",
        "current_step", "current_state_entry_id", "priority", "resume_id",
        "applied_date", "next_stage", "next_step", "next_date", "next_time",
        "next_note", "paused_from_stage", "pause_reason", "application_note",
    }
    if set(final_projection) != required_fields:
        raise RuntimeError("岗位合并最终投影字段不完整")

    timestamp = now_iso()
    timeline_entries = conn.execute(
        "UPDATE timeline_entries SET application_id = ? "
        "WHERE user_id = ? AND application_id = ?",
        (destination_id, user_id, source_id),
    ).rowcount
    questions = conn.execute(
        "UPDATE questions SET application_id = ?, company = ?, updated_time = ? "
        "WHERE user_id = ? AND application_id = ?",
        (destination_id, destination_company, timestamp, user_id, source_id),
    ).rowcount
    occurrences = conn.execute(
        "UPDATE review_question_occurrences SET application_id = ?, company = ? "
        "WHERE user_id = ? AND application_id = ?",
        (destination_id, destination_company, user_id, source_id),
    ).rowcount
    resumes = conn.execute(
        "UPDATE resumes SET application_id = ?, updated_time = ? "
        "WHERE user_id = ? AND application_id = ?",
        (destination_id, timestamp, user_id, source_id),
    ).rowcount

    changed = conn.execute(
        "UPDATE applications SET department = ?, channel = ?, jd_text = ?, "
        "jd_parsed_json = ?, stage = ?, current_step = ?, current_state_entry_id = ?, "
        "priority = ?, resume_id = ?, applied_date = ?, next_stage = ?, next_step = ?, "
        "next_date = ?, next_time = ?, next_note = ?, paused_from_stage = ?, "
        "pause_reason = ?, application_note = ?, prep_status = 'none', "
        "prep_generation = NULL, prep_heartbeat_time = NULL, prep_json = NULL, "
        "revision = revision + 1, updated_time = ? WHERE user_id = ? AND id = ?",
        (
            final_projection["department"],
            final_projection["channel"],
            final_projection["jd_text"],
            final_projection["jd_parsed_json"],
            final_projection["stage"],
            final_projection["current_step"],
            final_projection["current_state_entry_id"],
            final_projection["priority"],
            final_projection["resume_id"],
            final_projection["applied_date"],
            final_projection["next_stage"],
            final_projection["next_step"],
            final_projection["next_date"],
            final_projection["next_time"],
            final_projection["next_note"],
            final_projection["paused_from_stage"],
            final_projection["pause_reason"],
            final_projection["application_note"],
            timestamp,
            user_id,
            destination_id,
        ),
    ).rowcount
    if changed != 1:
        raise RuntimeError("岗位合并没有精确更新冻结目标")
    deleted = conn.execute(
        "DELETE FROM applications WHERE user_id = ? AND id = ?",
        (user_id, source_id),
    ).rowcount
    if deleted != 1:
        raise RuntimeError("岗位合并没有精确删除冻结源记录")
    return {
        "status": "ok",
        "source_application_id": source_id,
        "destination_application_id": destination_id,
        "moved": {
            "timeline_entries": timeline_entries,
            "questions": questions,
            "question_occurrences": occurrences,
            "resumes": resumes,
        },
    }


def _next_values(projection: dict) -> tuple:
    action = projection.get("next_action")
    if action is None:
        return (None, None, None, None, None)
    return (
        action["stage"], action["step"], action.get("date"),
        action.get("time"), action.get("note"),
    )


def _write_application_update_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
    *,
    created_time: str,
    expected_revision: int,
    expected: dict,
    replacement: dict,
    changed_fields: set[str],
    question_provenance: list[dict],
    occurrence_provenance: list[dict],
    reverse: bool,
    invalidate_prep: bool,
    occurred_date: str,
    timestamp: str,
) -> dict:
    if not conn.in_transaction:
        raise RuntimeError("application update executor 必须运行在显式写事务内")
    if not changed_fields or not changed_fields <= {
        "company", "position", "stage", "current_step", "priority", "next_action",
        "application_note", "jd_text",
    }:
        raise RuntimeError("application update executor 字段集不合法")

    questions_updated = 0
    for item in question_provenance:
        before_company = item["after_company"] if reverse else item["before_company"]
        after_company = item["before_company"] if reverse else item["after_company"]
        expected_updated_time = (
            item["after_updated_time"] if reverse else item["before_updated_time"]
        )
        changed = conn.execute(
            "UPDATE questions SET company = ?, updated_time = ? "
            "WHERE id = ? AND user_id = ? AND application_id = ? "
            "AND created_time = ? AND updated_time = ? AND company IS ?",
            (
                after_company,
                timestamp,
                item["id"],
                user_id,
                application_id,
                item["question_created_time"],
                expected_updated_time,
                before_company,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("application question provenance 未精确命中")
        questions_updated += 1

    occurrences_updated = 0
    for item in occurrence_provenance:
        before_company = item["after_company"] if reverse else item["before_company"]
        after_company = item["before_company"] if reverse else item["after_company"]
        changed = conn.execute(
            "UPDATE review_question_occurrences SET company = ? "
            "WHERE user_id = ? AND journal_id = ? AND question_id = ? "
            "AND application_id = ? AND company = ? AND source_step IS ? AND asked_date IS ?",
            (
                after_company,
                user_id,
                item["journal_id"],
                item["question_id"],
                application_id,
                before_company,
                item["source_step"],
                item["asked_date"],
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("application occurrence provenance 未精确命中")
        occurrences_updated += 1

    timeline_entry_id = None
    state_changed = bool({"stage", "current_step"} & changed_fields)
    normalized_occurred_date = validate_iso_date(occurred_date, label="历程日期")
    if state_changed and normalized_occurred_date is None:
        raise RuntimeError("application update 状态变化缺少可信本地日期")
    if state_changed:
        stage_changed = "stage" in changed_fields
        step_changed = "current_step" in changed_fields
        if stage_changed and step_changed:
            summary = (
                f"Agent 更新状态：阶段「{STAGE_LABELS[expected['stage']]}」→"
                f"「{STAGE_LABELS[replacement['stage']]}」，环节「"
                f"{expected.get('current_step') or '未设置'}」→「"
                f"{replacement.get('current_step') or '未设置'}」"
            )
        elif stage_changed:
            summary = (
                f"Agent 更新阶段：「{STAGE_LABELS[expected['stage']]}」→"
                f"「{STAGE_LABELS[replacement['stage']]}」"
            )
        else:
            summary = (
                f"Agent 更新当前环节：「{expected.get('current_step') or '未设置'}」→"
                f"「{replacement.get('current_step') or '未设置'}」"
            )
        if reverse:
            summary = f"撤销{summary}"
        entry = conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, step, occurred_date, "
            "summary, from_stage, from_step, to_stage, to_step, source, created_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'agent', ?)",
            (
                user_id,
                application_id,
                replacement.get("current_step"),
                normalized_occurred_date,
                summary,
                expected["stage"],
                expected.get("current_step"),
                replacement["stage"],
                replacement.get("current_step"),
                timestamp,
            ),
        )
        timeline_entry_id = int(entry.lastrowid)

    expected_next = _next_values(expected)
    replacement_next = _next_values(replacement)
    assignments = [
        "company = ?", "company_id = ?", "position = ?", "stage = ?",
        "current_step = ?", "applied_date = ?", "next_stage = ?",
        "next_step = ?", "next_date = ?", "next_time = ?", "next_note = ?",
        "paused_from_stage = ?", "pause_reason = ?", "application_note = ?",
        "priority = ?", "jd_text = ?", "revision = revision + 1", "updated_time = ?",
    ]
    invalidated_prep_json = None
    if invalidate_prep:
        prep_row = conn.execute(
            "SELECT prep_json FROM applications WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if prep_row is None:
            raise RuntimeError("application update executor 岗位不存在")
        invalidated_prep_json = _invalidated_semantic_prep_json(prep_row[0])
        assignments.extend((
            "prep_status = 'none'",
            "prep_generation = NULL",
            "prep_heartbeat_time = NULL",
            "prep_json = ?",
        ))
    parameters = [
        replacement["company"],
        replacement["company_id"],
        replacement["position"],
        replacement["stage"],
        replacement.get("current_step"),
        replacement.get("applied_date"),
        *replacement_next,
        replacement.get("paused_from_stage"),
        replacement.get("pause_reason"),
        replacement.get("application_note"),
        replacement.get("priority"),
        replacement.get("jd_text"),
        timestamp,
        user_id,
        application_id,
        created_time,
        expected_revision,
        expected["company"],
        expected["company_id"],
        expected["position"],
        expected["stage"],
        expected.get("current_step"),
        expected.get("applied_date"),
        *expected_next,
        expected.get("paused_from_stage"),
        expected.get("pause_reason"),
        expected.get("application_note"),
        expected.get("priority"),
        expected.get("jd_text"),
    ]
    if timeline_entry_id is not None:
        assignments.insert(5, "current_state_entry_id = ?")
        parameters.insert(5, timeline_entry_id)
    if invalidate_prep:
        # SET placeholders end after updated_time; insert before the WHERE parameters.
        parameters.insert(17 + (1 if timeline_entry_id is not None else 0),
                          invalidated_prep_json)
    where = (
        "WHERE user_id = ? AND id = ? AND created_time = ? AND revision = ? "
        "AND company = ? AND company_id IS ? AND position = ? AND stage = ? "
        "AND current_step IS ? AND applied_date IS ? AND next_stage IS ? AND next_step IS ? AND next_date IS ? "
        "AND next_time IS ? AND next_note IS ? AND paused_from_stage IS ? "
        "AND pause_reason IS ? AND application_note IS ? AND priority IS ? AND jd_text IS ?"
    )
    changed = conn.execute(
        f"UPDATE applications SET {', '.join(assignments)} {where}",
        parameters,
    ).rowcount
    if changed != 1:
        raise RuntimeError("application update executor 未精确命中冻结目标")
    return {
        "status": "ok",
        "application_id": application_id,
        "revision": expected_revision + 1,
        "timeline_entry_id": timeline_entry_id,
        "questions_updated": questions_updated,
        "question_occurrences_updated": occurrences_updated,
        "prep_invalidated": invalidate_prep,
    }


def _update_application_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
    **kwargs,
) -> dict:
    return _write_application_update_in_transaction(
        conn, user_id, application_id, reverse=False, **kwargs,
    )


def _undo_application_update_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
    **kwargs,
) -> dict:
    return _write_application_update_in_transaction(
        conn, user_id, application_id, reverse=True, **kwargs,
    )


def has_later_application_state_write_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
    *,
    source_journal_id: int,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM timeline_entries entry "
        "JOIN journal source ON source.id = ? AND source.user_id = entry.user_id "
        "WHERE entry.user_id = ? AND entry.application_id = ? "
        "AND (entry.from_stage != entry.to_stage OR entry.from_step IS NOT entry.to_step) "
        "AND entry.journal_id IS NOT ? AND entry.created_time >= source.created_time LIMIT 1",
        (source_journal_id, user_id, application_id, source_journal_id),
    ).fetchone()
    return row is not None


def restore_application_stage_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
    *,
    source_journal_id: int,
    expected_stage: str,
    replacement_stage: str,
) -> bool:
    if has_later_application_state_write_in_transaction(
        conn,
        user_id,
        application_id,
        source_journal_id=source_journal_id,
    ):
        return False
    row = conn.execute(
        "SELECT current_step, revision FROM applications "
        "WHERE user_id = ? AND id = ? AND stage = ?",
        (user_id, application_id, expected_stage),
    ).fetchone()
    if row is None:
        return False
    timestamp = now_iso()
    entry = conn.execute(
        "INSERT INTO timeline_entries (user_id, application_id, step, occurred_date, "
        "summary, from_stage, from_step, to_stage, to_step, source, created_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'system', ?)",
        (
            user_id,
            application_id,
            row[0],
            timestamp[:10],
            "撤销复盘对当前阶段的影响",
            expected_stage,
            row[0],
            replacement_stage,
            row[0],
            timestamp,
        ),
    )
    paused_from_stage = (
        expected_stage
        if replacement_stage == "pooled" and expected_stage in ACTIVE_STAGES
        else None
    )
    clear_next = replacement_stage in CLOSED_STAGES
    changed = conn.execute(
        "UPDATE applications SET stage = ?, current_state_entry_id = ?, "
        "paused_from_stage = ?, pause_reason = NULL, "
        "next_stage = CASE WHEN ? THEN NULL ELSE next_stage END, "
        "next_step = CASE WHEN ? THEN NULL ELSE next_step END, "
        "next_date = CASE WHEN ? THEN NULL ELSE next_date END, "
        "next_time = CASE WHEN ? THEN NULL ELSE next_time END, "
        "next_note = CASE WHEN ? THEN NULL ELSE next_note END, "
        "revision = revision + 1, updated_time = ? "
        "WHERE user_id = ? AND id = ? AND stage = ? AND revision = ?",
        (
            replacement_stage,
            int(entry.lastrowid),
            paused_from_stage,
            clear_next,
            clear_next,
            clear_next,
            clear_next,
            clear_next,
            timestamp,
            user_id,
            application_id,
            expected_stage,
            row[1],
        ),
    ).rowcount
    return changed == 1
