"""Read models for the application board, detail, and upcoming plan."""

from ....platform.database import loads_json, read_connection
from .prep import _prep_retry_after_seconds
from .shared import (
    BOARD_STAGES,
    timeline_entry_display_time,
    timeline_entry_snapshot_fingerprint,
)


_LAST_ACTIVITY_COLUMN = (
    "COALESCE((SELECT MAX(created_time) FROM timeline_entries "
    "WHERE timeline_entries.user_id = applications.user_id "
    "AND timeline_entries.application_id = applications.id), created_time)"
)

_APPLICATION_COLUMNS = (
    "id, company, position, department, channel, stage, current_step, "
    "next_stage, next_step, next_date, next_time, next_note, paused_from_stage, "
    "pause_reason, priority, applied_date, prep_status, prep_json, "
    "jd_parsed_json, jd_text, resume_id, updated_time, prep_heartbeat_time, "
    "revision, application_note, created_time"
)
_BOARD_COLUMNS = (
    "id, company, position, department, channel, stage, current_step, "
    "next_stage, next_step, next_date, next_time, next_note, paused_from_stage, "
    "pause_reason, priority, applied_date, prep_status, revision, created_time"
)


def _next_action(
    stage: str | None,
    step: str | None,
    next_date: str | None,
    next_time: str | None,
    note: str | None,
) -> dict | None:
    if step is None:
        return None
    return {
        "stage": stage,
        "step": step,
        "date": next_date,
        "time": next_time,
        "note": note,
    }


def _board_row_to_dict(row) -> dict:
    (
        application_id,
        company,
        position,
        department,
        channel,
        stage,
        current_step,
        next_stage,
        next_step,
        next_date,
        next_time,
        next_note,
        paused_from_stage,
        pause_reason,
        priority,
        applied_date,
        prep_status,
        revision,
        created_time,
    ) = row
    return {
        "id": application_id,
        "company": company,
        "position": position,
        "department": department,
        "channel": channel,
        "stage": stage,
        "current_step": current_step,
        "next_action": _next_action(
            next_stage, next_step, next_date, next_time, next_note,
        ),
        "paused_from_stage": paused_from_stage,
        "pause_reason": pause_reason,
        "priority": priority,
        "applied_date": applied_date,
        "prep_status": prep_status,
        "revision": revision,
        "created_time": created_time,
    }


def _application_row_to_dict(row) -> dict:
    (
        application_id, company, position, department, channel, stage, current_step,
        next_stage, next_step, next_date, next_time, next_note, paused_from_stage,
        pause_reason, priority, applied_date, prep_status, prep_json,
        jd_parsed_json, jd_text, resume_id, updated_time, heartbeat, revision,
        application_note, created_time,
    ) = row
    board_row = (
        application_id, company, position, department, channel, stage, current_step,
        next_stage, next_step, next_date, next_time, next_note, paused_from_stage,
        pause_reason, priority, applied_date, prep_status, revision, created_time,
    )
    detail = _board_row_to_dict(board_row)
    detail.update(
        {
            "prep": loads_json(prep_json, None),
            "jd_parsed": loads_json(jd_parsed_json, {}),
            "jd_text": jd_text,
            "resume_id": resume_id,
            "updated_time": updated_time,
            "application_note": application_note,
        }
    )
    detail["prep_retry_after_seconds"] = (
        _prep_retry_after_seconds(heartbeat or updated_time)
        if detail["prep_status"] in {"pending", "running"}
        else None
    )
    return detail


def _timeline_entry_dict(row, *, timezone_name: str) -> dict:
    (
        entry_id,
        step,
        occurred_date,
        outcome,
        summary,
        from_stage,
        from_step,
        to_stage,
        to_step,
        source,
        created_time,
    ) = row
    return {
        "id": entry_id,
        "step": step,
        "occurred_date": occurred_date,
        "outcome": outcome,
        "summary": summary,
        "from_stage": from_stage,
        "from_step": from_step,
        "to_stage": to_stage,
        "to_step": to_step,
        "source": source,
        "created_time": created_time,
        "display_time": timeline_entry_display_time(created_time, timezone_name),
        "snapshot_fingerprint": timeline_entry_snapshot_fingerprint(
            created_time=created_time,
            step=step,
            occurred_date=occurred_date,
            outcome=outcome,
            summary=summary,
            from_stage=from_stage,
            from_step=from_step,
            to_stage=to_stage,
            to_step=to_step,
            source=source,
        ),
    }


def board(db_path: str, user_id: str) -> dict:
    with read_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_BOARD_COLUMNS}, {_LAST_ACTIVITY_COLUMN} AS last_activity_time "
            "FROM applications WHERE user_id = ? "
            "ORDER BY CASE priority "
            "WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, "
            "created_time DESC, id DESC",
            (user_id,),
        ).fetchall()
    columns = {stage: [] for stage in BOARD_STAGES}
    for row in rows:
        *base, last_activity_time = row
        item = _board_row_to_dict(base)
        item["last_activity_time"] = last_activity_time
        columns[item["stage"]].append(item)
    return {"columns": columns, "total": len(rows)}


def statistics(db_path: str, user_id: str) -> dict:
    """Aggregate current outcomes and historically reached funnel stages."""
    with read_connection(db_path) as conn:
        applications = conn.execute(
            "SELECT id, stage, applied_date FROM applications WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        timeline_rows = conn.execute(
            "SELECT application_id, from_stage, to_stage FROM timeline_entries "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchall()

    reached_by_application: dict[int, set[str]] = {
        application_id: {stage}
        for application_id, stage, _applied_date in applications
    }
    for application_id, from_stage, to_stage in timeline_rows:
        reached = reached_by_application.setdefault(application_id, set())
        reached.update((from_stage, to_stage))

    submitted_stages = {"applied", "written_test", "interviewing", "offer"}
    submitted = 0
    written_test = 0
    interviewing = 0
    offers_reached = 0
    for application_id, _stage, applied_date in applications:
        reached = reached_by_application.get(application_id, set())
        has_submitted = applied_date is not None or bool(reached & submitted_stages)
        submitted += int(has_submitted)
        written_test += int("written_test" in reached)
        interviewing += int(bool(reached & {"interviewing", "offer"}))
        offers_reached += int("offer" in reached)

    current_counts = {
        stage: sum(1 for _application_id, current, _applied_date in applications if current == stage)
        for stage in BOARD_STAGES
    }
    active_processes = sum(current_counts[stage] for stage in (
        "applied", "written_test", "interviewing", "pooled",
    ))

    def conversion(numerator: int) -> float:
        return round(numerator * 100 / submitted, 1) if submitted else 0.0

    return {
        "total_positions": len(applications),
        "submitted": submitted,
        "active_processes": active_processes,
        "offers": current_counts["offer"],
        "rejected": current_counts["rejected"],
        "withdrawn": current_counts["withdrawn"],
        "pooled": current_counts["pooled"],
        "interview_conversion_percent": conversion(interviewing),
        "offer_conversion_percent": conversion(offers_reached),
        "funnel": {
            "submitted": submitted,
            "written_test": written_test,
            "interviewing": interviewing,
            "offer": offers_reached,
            "rejected": current_counts["rejected"],
        },
    }


def application_detail(
    db_path: str,
    user_id: str,
    application_id: int,
    *,
    timezone_name: str = "UTC",
) -> dict | None:
    with read_connection(db_path) as conn:
        # Pin both reads to one WAL snapshot; otherwise a concurrent projection
        # write can pair an old application revision with newer history rows.
        conn.execute("BEGIN")
        row = conn.execute(
            f"SELECT {_APPLICATION_COLUMNS} FROM applications "
            "WHERE user_id = ? AND id = ?",
            (user_id, application_id),
        ).fetchone()
        if row is None:
            return None
        timeline_rows = conn.execute(
            "SELECT id, step, occurred_date, outcome, summary, from_stage, from_step, "
            "to_stage, to_step, source, created_time FROM timeline_entries "
            "WHERE user_id = ? AND application_id = ? ORDER BY created_time, id",
            (user_id, application_id),
        ).fetchall()
    detail = _application_row_to_dict(row)
    detail["timeline_entries"] = [
        _timeline_entry_dict(item, timezone_name=timezone_name) for item in timeline_rows
    ]
    return detail


def upcoming(db_path: str, user_id: str, date_from: str, date_to: str) -> list[dict]:
    with read_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_BOARD_COLUMNS} FROM applications "
            "WHERE user_id = ? AND next_date IS NOT NULL "
            "AND next_date >= ? AND next_date <= ? "
            "AND stage NOT IN ('rejected', 'withdrawn', 'pooled') "
            "ORDER BY next_date, next_time, id",
            (user_id, date_from, date_to),
        ).fetchall()
    return [_board_row_to_dict(row) for row in rows]
