"""Atomic writes for application state, next action, and factual history."""

import re
from datetime import datetime
from sqlite3 import Connection, IntegrityError
from zoneinfo import ZoneInfo

from ...companies import public as companies
from ....platform.database import (
    INTERACTIVE_BUSY_TIMEOUT_MS,
    now_iso,
    read_connection,
    transaction,
)
from .board import application_detail
from .prep import _invalidated_semantic_prep_json
from .shared import (
    ACTIVE_STAGES,
    BOARD_STAGES,
    CLOSED_STAGES,
    STAGE_LABELS,
    TimelineMutationConflict,
    normalize_optional_text,
    timeline_entry_snapshot_fingerprint,
    validate_iso_date,
)


_OUTCOMES = frozenset({"passed", "failed", "cancelled"})
_SOURCES = frozenset({"manual", "agent", "review", "drag", "system"})
_CLOCK_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_UNSET = object()


def timeline_entry_source(
    db_path: str,
    user_id: str,
    application_id: int,
    entry_id: int,
) -> str | None:
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT source.kind FROM timeline_entries entry LEFT JOIN journal source "
            "ON source.id = entry.journal_id AND source.user_id = entry.user_id "
            "WHERE entry.id = ? AND entry.user_id = ? AND entry.application_id = ?",
            (entry_id, user_id, application_id),
        ).fetchone()
    if row is None:
        return None
    return "review" if row[0] == "review" else "manual"


def _projection_row(conn: Connection, user_id: str, application_id: int):
    return conn.execute(
        "SELECT company, position, stage, current_step, current_state_entry_id, "
        "next_stage, next_step, next_date, next_time, next_note, paused_from_stage, "
        "pause_reason, applied_date, revision FROM applications WHERE user_id = ? AND id = ?",
        (user_id, application_id),
    ).fetchone()


def _projection_dict(row) -> dict:
    (
        company,
        position,
        stage,
        current_step,
        current_state_entry_id,
        next_stage,
        next_step,
        next_date,
        next_time,
        next_note,
        paused_from_stage,
        pause_reason,
        applied_date,
        revision,
    ) = row
    return {
        "company": company,
        "position": position,
        "stage": stage,
        "current_step": current_step,
        "current_state_entry_id": current_state_entry_id,
        "next_action": (
            {
                "stage": next_stage,
                "step": next_step,
                "date": next_date,
                "time": next_time,
                "note": next_note,
            }
            if next_step is not None
            else None
        ),
        "paused_from_stage": paused_from_stage,
        "pause_reason": pause_reason,
        "applied_date": applied_date,
        "revision": revision,
    }


def _normalize_next_action(value) -> dict | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        raise ValueError("下一步必须是结构化对象")
    stage = value.get("stage")
    if stage not in BOARD_STAGES:
        raise ValueError("下一步阶段无效")
    step = normalize_optional_text(value.get("step"), limit=300)
    if step is None:
        raise ValueError("下一步必须填写具体环节")
    next_date = validate_iso_date(value.get("date"), label="下一步日期")
    next_time = value.get("time")
    if next_time is not None and (
        not isinstance(next_time, str) or _CLOCK_PATTERN.fullmatch(next_time) is None
    ):
        raise ValueError("下一步时间必须是 HH:MM")
    if next_time is not None and next_date is None:
        raise ValueError("下一步时间必须同时填写日期")
    note = normalize_optional_text(value.get("note"), limit=2_000)
    return {
        "stage": stage,
        "step": step,
        "date": next_date,
        "time": next_time,
        "note": note,
    }


def _next_action_columns(next_action: dict | None) -> tuple:
    if next_action is None:
        return (None, None, None, None, None)
    return (
        next_action["stage"],
        next_action["step"],
        next_action["date"],
        next_action["time"],
        next_action["note"],
    )


def _entry_fingerprint(row) -> str:
    return timeline_entry_snapshot_fingerprint(
        created_time=row[9],
        step=row[0],
        occurred_date=row[1],
        outcome=row[2],
        summary=row[3],
        from_stage=row[4],
        from_step=row[5],
        to_stage=row[6],
        to_step=row[7],
        source=row[8],
    )


def apply_application_progress_in_transaction(
    conn: Connection,
    user_id: str,
    application_id: int,
    *,
    expected_revision: int,
    step: str | None,
    occurred_date: str | None,
    outcome: str | None,
    summary: str | None,
    update_current_state: bool,
    target_stage: str | None,
    target_step: str | None,
    replace_next_action: bool = False,
    next_action=None,
    use_fact_step_for_current: bool = True,
    source: str = "manual",
    journal_id: int | None = None,
    timestamp: str | None = None,
) -> dict | None:
    """Apply one factual progress command inside the caller's transaction."""
    row = _projection_row(conn, user_id, application_id)
    if row is None:
        return None
    before = _projection_dict(row)
    if before["revision"] != expected_revision:
        raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
    if source not in _SOURCES:
        raise ValueError("历程来源无效")
    if target_stage is not None and target_stage not in BOARD_STAGES:
        raise ValueError("目标阶段无效")
    if outcome is not None and outcome not in _OUTCOMES:
        raise ValueError("历程结果无效")
    normalized_step = normalize_optional_text(step, limit=300)
    normalized_target_step = normalize_optional_text(target_step, limit=300)
    normalized_summary = normalize_optional_text(summary, limit=2_000)
    normalized_date = validate_iso_date(occurred_date, label="历程日期")
    if not update_current_state and (target_stage is not None or target_step is not None):
        raise ValueError("不更新当前状态时不能指定目标状态")

    if update_current_state:
        final_stage = target_stage or before["stage"]
        final_step = normalized_target_step
        if final_step is None:
            final_step = (
                normalized_step
                if use_fact_step_for_current and normalized_step is not None
                else before["current_step"]
            )
    else:
        final_stage = before["stage"]
        final_step = before["current_step"]

    if replace_next_action:
        final_next_action = _normalize_next_action(next_action)
    else:
        final_next_action = before["next_action"]
    if final_stage in CLOSED_STAGES:
        if replace_next_action and final_next_action is not None:
            raise ValueError("已挂或不再跟进的岗位不能保留下一步")
        final_next_action = None

    final_applied_date = before["applied_date"]
    if (
        final_stage == "applied"
        and before["stage"] != "applied"
        and final_applied_date is None
    ):
        final_applied_date = normalized_date

    state_changed = (
        final_stage != before["stage"] or final_step != before["current_step"]
    )
    if not any((normalized_step, normalized_summary, outcome, state_changed)):
        raise ValueError("进展至少要有环节、结果、说明或状态变化")

    timestamp = timestamp or now_iso()
    cursor = conn.execute(
        "INSERT INTO timeline_entries (user_id, application_id, step, occurred_date, "
        "outcome, summary, from_stage, from_step, to_stage, to_step, source, journal_id, "
        "created_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            application_id,
            normalized_step,
            normalized_date,
            outcome,
            normalized_summary,
            before["stage"],
            before["current_step"],
            final_stage,
            final_step,
            source,
            journal_id,
            timestamp,
        ),
    )
    entry_id = int(cursor.lastrowid)

    if final_stage == "pooled":
        paused_from_stage = (
            before["stage"]
            if before["stage"] in ACTIVE_STAGES
            else before["paused_from_stage"]
        )
        pause_reason = before["pause_reason"] if before["stage"] == "pooled" else None
    else:
        paused_from_stage = None
        pause_reason = None
    current_state_entry_id = (
        entry_id if state_changed else before["current_state_entry_id"]
    )
    next_values = _next_action_columns(final_next_action)
    updated = conn.execute(
        "UPDATE applications SET stage = ?, current_step = ?, current_state_entry_id = ?, "
        "next_stage = ?, next_step = ?, next_date = ?, next_time = ?, next_note = ?, "
        "paused_from_stage = ?, pause_reason = ?, applied_date = ?, "
        "revision = revision + 1, updated_time = ? "
        "WHERE user_id = ? AND id = ? AND revision = ?",
        (
            final_stage,
            final_step,
            current_state_entry_id,
            *next_values,
            paused_from_stage,
            pause_reason,
            final_applied_date,
            timestamp,
            user_id,
            application_id,
            expected_revision,
        ),
    ).rowcount
    if updated != 1:
        raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")

    after = {
        **before,
        "stage": final_stage,
        "current_step": final_step,
        "current_state_entry_id": current_state_entry_id,
        "next_action": final_next_action,
        "paused_from_stage": paused_from_stage,
        "pause_reason": pause_reason,
        "applied_date": final_applied_date,
        "revision": expected_revision + 1,
    }
    return {
        "entry_id": entry_id,
        "revision": expected_revision + 1,
        "before": before,
        "after": after,
    }


def create_application_profile(
    db_path: str,
    user_id: str,
    *,
    company: str,
    position: str,
    department: str | None,
    channel: str | None,
    stage: str,
    current_step: str | None,
    applied_date: str | None = None,
    pause_reason: str | None = None,
    next_action,
    jd_text: str | None,
    priority: str | None = None,
    timezone_name: str = "UTC",
) -> int:
    if stage not in BOARD_STAGES:
        raise ValueError("岗位阶段无效")
    if priority not in {None, "high", "medium", "low"}:
        raise ValueError("岗位优先级无效")
    normalized_next = _normalize_next_action(next_action)
    if stage in CLOSED_STAGES and normalized_next is not None:
        raise ValueError("已挂或不再跟进的岗位不能保留下一步")
    normalized_pause_reason = normalize_optional_text(pause_reason, limit=1_000)
    if stage != "pooled" and normalized_pause_reason is not None:
        raise ValueError("只有泡池子阶段可以填写暂停原因")
    timestamp = now_iso()
    local_date = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    normalized_applied_date = validate_iso_date(applied_date, label="投递日期")
    if stage == "applied" and normalized_applied_date is None:
        normalized_applied_date = local_date
    try:
        with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
            conn.execute("BEGIN IMMEDIATE")
            company_id = companies.ensure_company_in_transaction(conn, user_id, company)
            cursor = conn.execute(
                "INSERT INTO applications (user_id, company, company_id, position, department, channel, "
                "jd_text, stage, priority, current_step, applied_date, pause_reason, next_stage, next_step, next_date, "
                "next_time, next_note, created_time, updated_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    company,
                    company_id,
                    position,
                    department,
                    channel,
                    jd_text,
                    stage,
                    priority,
                    current_step,
                    normalized_applied_date,
                    normalized_pause_reason,
                    *_next_action_columns(normalized_next),
                    timestamp,
                    timestamp,
                ),
            )
            application_id = int(cursor.lastrowid)
            if stage != "backlog" or current_step is not None:
                entry = conn.execute(
                    "INSERT INTO timeline_entries (user_id, application_id, step, "
                    "occurred_date, summary, from_stage, from_step, to_stage, to_step, "
                    "source, created_time) VALUES (?, ?, ?, ?, ?, 'backlog', NULL, ?, ?, "
                    "'manual', ?)",
                    (
                        user_id,
                        application_id,
                        current_step,
                        normalized_applied_date if stage == "applied" else local_date,
                        f"新增岗位并设为「{STAGE_LABELS[stage]}」",
                        stage,
                        current_step,
                        timestamp,
                    ),
                )
                conn.execute(
                    "UPDATE applications SET current_state_entry_id = ? WHERE id = ?",
                    (int(entry.lastrowid), application_id),
                )
            return application_id
    except IntegrityError as error:
        if "applications.user_id, applications.company_key, applications.position_key" in str(error):
            raise TimelineMutationConflict("同一公司和岗位已存在") from error
        raise


def update_application_profile(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    expected_revision: int,
    company: str,
    position: str,
    department: str | None,
    channel: str | None,
    stage: str,
    current_step: str | None,
    applied_date: str | None | object = _UNSET,
    pause_reason: str | None | object = _UNSET,
    next_action,
    jd_text: str | None,
    priority: str | None | object = _UNSET,
    timezone_name: str = "UTC",
) -> bool | None:
    if stage not in BOARD_STAGES:
        raise ValueError("岗位阶段无效")
    if priority is not _UNSET and priority not in {None, "high", "medium", "low"}:
        raise ValueError("岗位优先级无效")
    normalized_next = _normalize_next_action(next_action)
    if stage in CLOSED_STAGES:
        normalized_next = None
    if stage != "pooled" and pause_reason is not _UNSET and pause_reason is not None:
        raise ValueError("只有泡池子阶段可以填写暂停原因")
    timestamp = now_iso()
    local_date = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    try:
        with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
            conn.execute("BEGIN IMMEDIATE")
            projection_row = _projection_row(conn, user_id, application_id)
            if projection_row is None:
                return None
            before = _projection_dict(projection_row)
            if before["revision"] != expected_revision:
                raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
            profile_row = conn.execute(
                "SELECT department, channel, jd_text, prep_json, priority FROM applications "
                "WHERE user_id = ? AND id = ?",
                (user_id, application_id),
            ).fetchone()
            if profile_row is None:
                return None
            before_department, _before_channel, before_jd_text, before_prep_json, before_priority = profile_row
            projected_priority = before_priority if priority is _UNSET else priority
        # Invalidate preparation only when semantic inputs change. Stage, current step,
        # next action, and channel are not inputs and must not erase caches.
            prep_invalidated = any((
                company != before["company"],
                position != before["position"],
                department != before_department,
                jd_text != before_jd_text,
            ))
            company_id = companies.ensure_company_in_transaction(conn, user_id, company)
            state_changed = stage != before["stage"] or current_step != before["current_step"]
            projected_applied_date = (
                before["applied_date"]
                if applied_date is _UNSET
                else validate_iso_date(applied_date, label="投递日期")
            )
            if (
                stage == "applied"
                and before["stage"] != "applied"
                and projected_applied_date is None
            ):
                projected_applied_date = local_date
            projected_pause_reason = (
                before["pause_reason"]
                if stage == "pooled" and pause_reason is _UNSET
                else normalize_optional_text(pause_reason, limit=1_000)
                if stage == "pooled"
                else None
            )
            entry_id = before["current_state_entry_id"]
            if state_changed:
                cursor = conn.execute(
                    "INSERT INTO timeline_entries (user_id, application_id, step, "
                    "occurred_date, summary, from_stage, from_step, to_stage, to_step, "
                    "source, created_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?)",
                    (
                        user_id,
                        application_id,
                        current_step,
                        (
                            projected_applied_date
                            if stage == "applied" and projected_applied_date is not None
                            else local_date
                        ),
                        "在岗位详情中更新当前状态",
                        before["stage"],
                        before["current_step"],
                        stage,
                        current_step,
                        timestamp,
                    ),
                )
                entry_id = int(cursor.lastrowid)
            paused_from_stage = (
                before["stage"] if stage == "pooled" and before["stage"] in ACTIVE_STAGES
                else before["paused_from_stage"] if stage == "pooled"
                else None
            )
            updated = conn.execute(
                "UPDATE applications SET company = ?, company_id = ?, position = ?, department = ?, "
                "channel = ?, stage = ?, current_step = ?, current_state_entry_id = ?, "
                "applied_date = ?, "
                "next_stage = ?, next_step = ?, next_date = ?, next_time = ?, next_note = ?, "
                "paused_from_stage = ?, pause_reason = ?, priority = ?, jd_text = ?, "
                "prep_status = CASE WHEN ? THEN 'none' ELSE prep_status END, "
                "prep_generation = CASE WHEN ? THEN NULL ELSE prep_generation END, "
                "prep_heartbeat_time = CASE WHEN ? THEN NULL ELSE prep_heartbeat_time END, "
                "prep_json = CASE WHEN ? THEN ? ELSE prep_json END, "
                "revision = revision + 1, updated_time = ? "
                "WHERE user_id = ? AND id = ? AND revision = ?",
                (
                    company,
                    company_id,
                    position,
                    department,
                    channel,
                    stage,
                    current_step,
                    entry_id,
                    projected_applied_date,
                    *_next_action_columns(normalized_next),
                    paused_from_stage,
                    projected_pause_reason,
                    projected_priority,
                    jd_text,
                    prep_invalidated,
                    prep_invalidated,
                    prep_invalidated,
                    prep_invalidated,
                    _invalidated_semantic_prep_json(before_prep_json),
                    timestamp,
                    user_id,
                    application_id,
                    expected_revision,
                ),
            ).rowcount
            if updated != 1:
                raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
            if company != before["company"]:
                conn.execute(
                    "UPDATE questions SET company = ?, updated_time = ? "
                    "WHERE user_id = ? AND application_id = ?",
                    (company, timestamp, user_id, application_id),
                )
                conn.execute(
                    "UPDATE review_question_occurrences SET company = ? "
                    "WHERE user_id = ? AND application_id = ?",
                    (company, user_id, application_id),
                )
            return True
    except IntegrityError as error:
        if "applications.user_id, applications.company_key, applications.position_key" in str(error):
            raise TimelineMutationConflict("同一公司和岗位已存在") from error
        raise


def move_application_stage(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    expected_revision: int,
    stage: str,
    origin: str = "board_drag",
    timezone_name: str = "UTC",
) -> dict | None:
    if stage not in BOARD_STAGES:
        raise ValueError("岗位阶段无效")
    if origin not in {"board_drag", "detail_menu"}:
        raise ValueError("阶段变更来源无效")
    local_date = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _projection_row(conn, user_id, application_id)
        if row is None:
            return None
        before = _projection_dict(row)
        if before["revision"] != expected_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        from_stage = before["stage"]
        if stage != from_stage:
            result = apply_application_progress_in_transaction(
                conn,
                user_id,
                application_id,
                expected_revision=expected_revision,
                step=None,
                occurred_date=local_date,
                outcome=None,
                summary=(
                    f"从「{STAGE_LABELS[from_stage]}」拖到「{STAGE_LABELS[stage]}」"
                    if origin == "board_drag"
                    else f"阶段从「{STAGE_LABELS[from_stage]}」调整为「{STAGE_LABELS[stage]}」"
                ),
                update_current_state=True,
                target_stage=stage,
                target_step=None,
                source="drag" if origin == "board_drag" else "manual",
            )
            if result is None:  # pragma: no cover
                return None
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)


def set_application_next_action(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    expected_revision: int,
    next_action,
    timezone_name: str = "UTC",
) -> dict | None:
    normalized = _normalize_next_action(next_action)
    timestamp = now_iso()
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _projection_row(conn, user_id, application_id)
        if row is None:
            return None
        before = _projection_dict(row)
        if before["revision"] != expected_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        if before["stage"] in CLOSED_STAGES and normalized is not None:
            raise ValueError("已挂或不再跟进的岗位不能保留下一步")
        updated = conn.execute(
            "UPDATE applications SET next_stage = ?, next_step = ?, next_date = ?, "
            "next_time = ?, next_note = ?, revision = revision + 1, updated_time = ? "
            "WHERE user_id = ? AND id = ? AND revision = ?",
            (*_next_action_columns(normalized), timestamp, user_id, application_id, expected_revision),
        ).rowcount
        if updated != 1:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)


def record_application_progress(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    expected_revision: int,
    step: str | None,
    occurred_date: str | None,
    outcome: str | None,
    summary: str | None,
    update_current_state: bool,
    target_stage: str | None,
    target_step: str | None,
    replace_next_action: bool,
    next_action,
    timezone_name: str = "UTC",
    source: str = "manual",
    journal_id: int | None = None,
) -> dict | None:
    effective_occurred_date = occurred_date
    if update_current_state and target_stage == "applied" and effective_occurred_date is None:
        effective_occurred_date = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = apply_application_progress_in_transaction(
            conn,
            user_id,
            application_id,
            expected_revision=expected_revision,
            step=step,
            occurred_date=effective_occurred_date,
            outcome=outcome,
            summary=summary,
            update_current_state=update_current_state,
            target_stage=target_stage,
            target_step=target_step,
            replace_next_action=replace_next_action,
            next_action=next_action,
            source=source,
            journal_id=journal_id,
        )
        if result is None:
            return None
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)


def complete_application_next_action(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    expected_revision: int,
    occurred_date: str | None,
    outcome: str | None,
    summary: str | None,
    next_action,
    timezone_name: str = "UTC",
) -> dict | None:
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _projection_row(conn, user_id, application_id)
        if row is None:
            return None
        before = _projection_dict(row)
        if before["revision"] != expected_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        current_next = before["next_action"]
        if current_next is None:
            raise TimelineMutationConflict("该岗位当前没有可完成的下一步")
        effective_occurred_date = occurred_date
        target_stage = "rejected" if outcome == "failed" else current_next["stage"]
        if target_stage == "applied" and effective_occurred_date is None:
            effective_occurred_date = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
        result = apply_application_progress_in_transaction(
            conn,
            user_id,
            application_id,
            expected_revision=expected_revision,
            step=current_next["step"],
            occurred_date=effective_occurred_date,
            outcome=outcome,
            summary=summary or f"完成「{current_next['step']}」",
            update_current_state=True,
            target_stage=target_stage,
            target_step=current_next["step"],
            replace_next_action=True,
            next_action=None if outcome == "failed" else next_action,
            source="manual",
        )
        if result is None:  # pragma: no cover
            return None
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)


def set_application_note(
    db_path: str,
    user_id: str,
    application_id: int,
    note: str | None,
    *,
    expected_revision: int,
    timezone_name: str = "UTC",
) -> dict | None:
    normalized = normalize_optional_text(note, limit=2_000)
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            "UPDATE applications SET application_note = ?, revision = revision + 1, "
            "updated_time = ? WHERE user_id = ? AND id = ? AND revision = ?",
            (normalized, now_iso(), user_id, application_id, expected_revision),
        ).rowcount
        if updated == 0:
            exists = conn.execute(
                "SELECT 1 FROM applications WHERE user_id = ? AND id = ?",
                (user_id, application_id),
            ).fetchone()
            if exists is None:
                return None
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)


def append_application_note(
    db_path: str,
    user_id: str,
    application_id: int,
    note: str,
    *,
    expected_revision: int,
    timezone_name: str = "UTC",
) -> dict | None:
    """Atomically append to the job note for existing non-Agent callers."""
    addition = normalize_optional_text(note, limit=2_000)
    if addition is None:
        raise ValueError("追加备注不能为空")
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT application_note, revision FROM applications WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if row is None:
            return None
        if row[1] != expected_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        combined = f"{row[0]}\n{addition}" if row[0] else addition
        if len(combined) > 2_000:
            raise ValueError("岗位备注不能超过 2000 个字符")
        conn.execute(
            "UPDATE applications SET application_note = ?, revision = revision + 1, "
            "updated_time = ? WHERE user_id = ? AND id = ? AND revision = ?",
            (combined, now_iso(), user_id, application_id, expected_revision),
        )
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)


def update_timeline_entry(
    db_path: str,
    user_id: str,
    application_id: int,
    entry_id: int,
    *,
    expected_revision: int,
    expected_fingerprint: str,
    step: str | None,
    occurred_date: str | None,
    outcome: str | None,
    summary: str | None,
    timezone_name: str = "UTC",
) -> dict | None:
    normalized_step = normalize_optional_text(step, limit=300)
    normalized_summary = normalize_optional_text(summary, limit=2_000)
    normalized_date = validate_iso_date(occurred_date, label="历程日期")
    if outcome is not None and outcome not in _OUTCOMES:
        raise ValueError("历程结果无效")
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        application = conn.execute(
            "SELECT revision, current_state_entry_id FROM applications "
            "WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if application is None:
            return None
        if application[0] != expected_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        row = conn.execute(
            "SELECT step, occurred_date, outcome, summary, from_stage, from_step, "
            "to_stage, to_step, source, created_time FROM timeline_entries "
            "WHERE id = ? AND user_id = ? AND application_id = ?",
            (entry_id, user_id, application_id),
        ).fetchone()
        if row is None:
            return None
        if _entry_fingerprint(row) != expected_fingerprint:
            raise TimelineMutationConflict("该历程已被修改，请刷新后重试")
        # `step` is the factual label shown in history.  It only owns the live
        # projection when this entry originally advanced the step to that same
        # value.  A stage-only drag is also a current-state entry, but editing
        # its factual label must never rewrite `current_step`.
        step_drives_projection = (
            application[1] == entry_id
            and row[5] != row[7]
            and row[0] == row[7]
        )
        to_step = normalized_step if step_drives_projection else row[7]
        if not any((normalized_step, normalized_summary, outcome, row[4] != row[6], row[5] != to_step)):
            raise ValueError("历程不能为空")
        conn.execute(
            "UPDATE timeline_entries SET step = ?, occurred_date = ?, outcome = ?, "
            "summary = ?, to_step = ? WHERE id = ? AND user_id = ? AND application_id = ?",
            (
                normalized_step,
                normalized_date,
                outcome,
                normalized_summary,
                to_step,
                entry_id,
                user_id,
                application_id,
            ),
        )
        assignments = "revision = revision + 1, updated_time = ?"
        parameters: list = [now_iso()]
        if step_drives_projection:
            assignments = f"current_step = ?, {assignments}"
            parameters.insert(0, to_step)
        conn.execute(
            f"UPDATE applications SET {assignments} WHERE user_id = ? AND id = ? AND revision = ?",
            (*parameters, user_id, application_id, expected_revision),
        )
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)


def delete_timeline_entry(
    db_path: str,
    user_id: str,
    application_id: int,
    entry_id: int,
    *,
    expected_revision: int,
    expected_fingerprint: str,
    timezone_name: str = "UTC",
) -> dict | None:
    with transaction(db_path, busy_timeout_ms=INTERACTIVE_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        application = conn.execute(
            "SELECT revision, current_state_entry_id, next_stage, next_step, next_date, "
            "next_time, next_note, paused_from_stage, pause_reason, stage "
            "FROM applications "
            "WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if application is None:
            return None
        if application[0] != expected_revision:
            raise TimelineMutationConflict("岗位已在其他窗口修改，请刷新后重试")
        row = conn.execute(
            "SELECT step, occurred_date, outcome, summary, from_stage, from_step, "
            "to_stage, to_step, source, created_time FROM timeline_entries "
            "WHERE id = ? AND user_id = ? AND application_id = ?",
            (entry_id, user_id, application_id),
        ).fetchone()
        if row is None:
            return None
        if _entry_fingerprint(row) != expected_fingerprint:
            raise TimelineMutationConflict("该历程已被修改，请刷新后重试")
        if application[1] == entry_id:
            # A restored projection may only point at an entry that actually
            # produced that exact state.  A merely earlier transition can be
            # left behind after another history entry was deleted and would
            # make the pointer disagree with the application projection.
            previous = conn.execute(
                "SELECT id FROM timeline_entries WHERE user_id = ? AND application_id = ? "
                "AND id != ? AND (from_stage != to_stage OR from_step IS NOT to_step) "
                "AND to_stage = ? AND to_step IS ? "
                "AND (created_time < ? OR (created_time = ? AND id < ?)) "
                "ORDER BY created_time DESC, id DESC LIMIT 1",
                (
                    user_id,
                    application_id,
                    entry_id,
                    row[4],
                    row[5],
                    row[9],
                    row[9],
                    entry_id,
                ),
            ).fetchone()
            restored_stage = row[4]
            if restored_stage in CLOSED_STAGES:
                restored_next_action = (None, None, None, None, None)
            else:
                # The schedule is an independent projection.  Deleting the
                # history row that currently drives stage/step must not erase
                # a plan that may have been added or edited afterwards.
                restored_next_action = tuple(application[index] for index in range(2, 7))
            if restored_stage == "pooled":
                restored_paused_from_stage = application[7]
                restored_pause_reason = application[8]
                if restored_paused_from_stage is None:
                    # A resume transition clears pause metadata because the
                    # live stage is no longer pooled.  When that transition is
                    # deleted, recover the stage from the latest transition
                    # that actually entered the pool.
                    pause_source = conn.execute(
                        "SELECT from_stage FROM timeline_entries "
                        "WHERE user_id = ? AND application_id = ? AND id != ? "
                        "AND to_stage = 'pooled' AND from_stage != 'pooled' "
                        "AND (created_time < ? OR (created_time = ? AND id < ?)) "
                        "ORDER BY created_time DESC, id DESC LIMIT 1",
                        (
                            user_id,
                            application_id,
                            entry_id,
                            row[9],
                            row[9],
                            entry_id,
                        ),
                    ).fetchone()
                    if pause_source is not None and pause_source[0] in ACTIVE_STAGES:
                        restored_paused_from_stage = pause_source[0]
            else:
                restored_paused_from_stage = None
                restored_pause_reason = None
            conn.execute(
                "UPDATE applications SET stage = ?, current_step = ?, "
                "current_state_entry_id = ?, next_stage = ?, next_step = ?, "
                "next_date = ?, next_time = ?, next_note = ?, "
                "paused_from_stage = ?, pause_reason = ?, revision = revision + 1, "
                "updated_time = ? WHERE user_id = ? AND id = ? AND revision = ?",
                (
                    restored_stage,
                    row[5],
                    previous[0] if previous else None,
                    *restored_next_action,
                    restored_paused_from_stage,
                    restored_pause_reason,
                    now_iso(),
                    user_id,
                    application_id,
                    expected_revision,
                ),
            )
        else:
            conn.execute(
                "UPDATE applications SET revision = revision + 1, updated_time = ? "
                "WHERE user_id = ? AND id = ? AND revision = ?",
                (now_iso(), user_id, application_id, expected_revision),
            )
        conn.execute(
            "DELETE FROM timeline_entries WHERE id = ? AND user_id = ? AND application_id = ?",
            (entry_id, user_id, application_id),
        )
    return application_detail(db_path, user_id, application_id, timezone_name=timezone_name)
