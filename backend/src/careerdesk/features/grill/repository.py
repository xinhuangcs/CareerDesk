"""Persistence for Grill sessions referencing immutable question-set items."""

import json
from datetime import date, timedelta
from importlib.metadata import PackageNotFoundError, version as package_version

from ...platform.database import loads_json, now_iso, read_connection, transaction


def _application_version() -> str:
    try:
        return package_version("careerdesk")
    except PackageNotFoundError:
        return "development"


def claim_experiment_intro(
    db_path: str,
    user_id: str,
    *,
    release_version: str | None = None,
) -> tuple[bool, str]:
    """Claim the lab introduction once for the current application release."""
    current_release = release_version or _application_version()
    if not current_release or len(current_release) > 64:
        raise RuntimeError("invalid CareerDesk release version")
    key = f"ui.grill_experiment_intro_shown.v3:{current_release}:{user_id}"
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, 'shown')", (key,),
        )
        return cursor.rowcount == 1, current_release


def _question(row) -> dict:
    return {"id": row[0], "text": row[1], "category": row[2], "channel": row[3],
            "response_format": row[4], "difficulty": row[5], "primary_competency": row[6],
            "secondary_tags": loads_json(row[7], [])}


def create_session_in_transaction(conn, user_id: str, *, question_set_id: int,
                                  question_count: int) -> tuple[int, dict, int]:
    """Create a session while an orchestrator holds the currentness snapshot."""
    if question_count <= 0:
        raise ValueError("本场题量必须大于 0")
    if not getattr(conn, "in_transaction", False):
        raise ValueError("session creation requires an active transaction")
    if conn.execute("SELECT 1 FROM grill_sessions WHERE user_id = ? AND state = 'active'",
                    (user_id,)).fetchone():
        raise ValueError("已有进行中场次，请先完成或挂起")
    pack = conn.execute(
        "SELECT kind, edition, context_label, input_receipt_json FROM question_sets WHERE user_id = ? AND id = ? "
        "AND state = 'ready' AND archived_at IS NULL", (user_id, question_set_id),
    ).fetchone()
    if pack is None:
        raise ValueError("题集不存在、未就绪或已归档")
    unpracticed = conn.execute(
        "SELECT item.id FROM question_set_items item WHERE item.user_id = ? "
        "AND item.question_set_id = ? AND NOT EXISTS (SELECT 1 FROM grill_session_items prior_item "
        "JOIN grill_answers prior_answer ON prior_answer.session_item_id = prior_item.id "
        "WHERE prior_item.user_id = ? AND prior_item.question_set_item_id = item.id) "
        "ORDER BY item.ordinal LIMIT ?",
        (user_id, question_set_id, user_id, question_count),
    ).fetchall()
    items = unpracticed or conn.execute(
        "SELECT id FROM question_set_items WHERE user_id = ? AND question_set_id = ? "
        "ORDER BY ordinal LIMIT ?", (user_id, question_set_id, question_count),
    ).fetchall()
    if not items:
        raise ValueError("这份题集没有可练习的题目，请生成新题集")
    timestamp = now_iso()
    receipt = loads_json(pack[3], {})
    content_locale = receipt.get("content_locale") if isinstance(receipt, dict) else None
    if content_locale not in {"zh-CN", "en"}:
        content_locale = "zh-CN"
    plan = {"current_ordinal": 0, "total": len(items), "transcript": [],
            "follow_up_used": False, "content_locale": content_locale}
    cursor = conn.execute(
        "INSERT INTO grill_sessions (user_id, question_set_id, kind, edition, context_label, state, "
        "plan_json, started_time, updated_time) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (user_id, question_set_id, pack[0], pack[1], pack[2],
         json.dumps(plan, ensure_ascii=False), timestamp, timestamp),
    )
    session_id = cursor.lastrowid
    for ordinal, (set_item_id,) in enumerate(items):
        conn.execute(
            "INSERT INTO grill_session_items (user_id, session_id, question_set_item_id, ordinal) "
            "VALUES (?, ?, ?, ?)", (user_id, session_id, set_item_id, ordinal),
        )
    row = conn.execute(
        "SELECT si.id, qi.text, qi.category, qi.channel, qi.response_format, qi.difficulty, "
        "qi.primary_competency, qi.secondary_tags_json FROM grill_session_items si "
        "JOIN question_set_items qi ON qi.id = si.question_set_item_id "
        "WHERE si.session_id = ? AND si.ordinal = 0", (session_id,),
    ).fetchone()
    return session_id, _question(row), len(items)


def create_session(db_path: str, user_id: str, *, question_set_id: int,
                   question_count: int) -> tuple[int, dict, int]:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return create_session_in_transaction(
            conn, user_id, question_set_id=question_set_id, question_count=question_count,
        )


def get_session(db_path: str, user_id: str, session_id: int) -> dict | None:
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, question_set_id, kind, edition, context_label, state, plan_json, summary_json, "
            "started_time, ended_time FROM grill_sessions WHERE user_id = ? AND id = ?",
            (user_id, session_id),
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "question_set_id": row[1], "kind": row[2], "edition": row[3],
            "context_label": row[4], "state": row[5], "plan": loads_json(row[6], {}),
            "summary": loads_json(row[7], None), "started_time": row[8], "ended_time": row[9]}


def current_item(db_path: str, user_id: str, session_id: int) -> dict | None:
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT si.id, qi.text, qi.category, qi.channel, qi.response_format, qi.difficulty, "
            "qi.primary_competency, qi.secondary_tags_json, qi.evaluation_kind, qi.rubric_json, "
            "qi.answer_authority, COALESCE(si.session_owned_guide_json, qi.answer_guide_json), "
            "qi.evidence_json, qi.follow_up_allowed, qi.repeat_scope, si.follow_up_count, si.state, "
            "si.claim_token, si.claim_error_code "
            "FROM grill_sessions s JOIN grill_session_items si ON si.session_id = s.id "
            "JOIN question_set_items qi ON qi.id = si.question_set_item_id "
            "WHERE s.user_id = ? AND s.id = ? AND si.ordinal = json_extract(s.plan_json, '$.current_ordinal')",
            (user_id, session_id),
        ).fetchone()
    if row is None:
        return None
    value = _question(row[:8])
    value.update({"evaluation_kind": row[8], "rubric": loads_json(row[9], {}),
                  "answer_authority": row[10], "answer_guide": loads_json(row[11], {}),
                  "evidence": loads_json(row[12], []), "follow_up_allowed": bool(row[13]),
                  "repeat_scope": row[14], "follow_up_count": row[15], "state": row[16],
                  "claim_token": row[17], "claim_error_code": row[18]})
    return value


def claim_current_item(db_path: str, user_id: str, session_id: int, session_item_id: int,
                       token: str) -> dict:
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT s.state, s.plan_json, si.id, si.claim_token FROM grill_sessions s "
            "LEFT JOIN grill_session_items si ON si.session_id = s.id "
            "AND si.ordinal = json_extract(s.plan_json, '$.current_ordinal') "
            "WHERE s.user_id = ? AND s.id = ?", (user_id, session_id),
        ).fetchone()
        if row is None or row[0] != "active":
            return {"status": "inactive"}
        if row[2] != session_item_id:
            return {"status": "advanced"}
        if row[3] is not None:
            return {"status": "busy"}
        conn.execute("UPDATE grill_session_items SET claim_token = ?, claim_started_time = ?, "
                     "claim_error_code = NULL "
                     "WHERE user_id = ? AND id = ? AND claim_token IS NULL",
                     (token, timestamp, user_id, session_item_id))
        return {"status": "claimed", "plan": loads_json(row[1], {})}


def release_claim(db_path: str, user_id: str, session_item_id: int, token: str) -> None:
    with transaction(db_path) as conn:
        conn.execute("UPDATE grill_session_items SET claim_token = NULL, claim_started_time = NULL "
                     "WHERE user_id = ? AND id = ? AND claim_token = ?", (user_id, session_item_id, token))


def fail_claim(db_path: str, user_id: str, session_item_id: int, token: str, code: str) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE grill_session_items SET claim_token = NULL, claim_started_time = NULL, "
            "claim_error_code = ? WHERE user_id = ? AND id = ? AND claim_token = ?",
            (code[:100], user_id, session_item_id, token),
        )


def save_follow_up(db_path: str, user_id: str, session_id: int, session_item_id: int,
                   token: str, *, transcript: list) -> bool:
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT plan_json FROM grill_sessions WHERE user_id = ? AND id = ? AND state = 'active'",
                           (user_id, session_id)).fetchone()
        owned = conn.execute("SELECT 1 FROM grill_session_items WHERE user_id = ? AND id = ? "
                             "AND session_id = ? AND claim_token = ? AND follow_up_count = 0",
                             (user_id, session_item_id, session_id, token)).fetchone()
        if row is None or owned is None:
            return False
        plan = loads_json(row[0], {})
        plan["transcript"] = transcript
        plan["follow_up_used"] = True
        conn.execute("UPDATE grill_session_items SET follow_up_count = 1, claim_token = NULL, "
                     "claim_started_time = NULL WHERE id = ?", (session_item_id,))
        conn.execute("UPDATE grill_sessions SET plan_json = ?, updated_time = ? WHERE id = ?",
                     (json.dumps(plan, ensure_ascii=False), timestamp, session_id))
        return True


def _update_progress(conn, *, user_id: str, session_id: int, item: dict,
                     verdict: str, today: str) -> None:
    if verdict in {"ungradable", "skipped"}:
        return
    repeat_scope = item["repeat_scope"]
    if repeat_scope == "none":
        return
    context = conn.execute("SELECT question_set_id, context_label "
                           "FROM grill_sessions WHERE id = ?",
                           (session_id,)).fetchone()
    pack = conn.execute("SELECT resume_id, application_id FROM question_sets WHERE id = ?",
                        (context[0],)).fetchone()
    scope_kind = repeat_scope
    scope_ref_value = "global" if scope_kind == "global" else (
        pack[0] if scope_kind == "resume" else pack[1]
    )
    if scope_ref_value is None:
        return
    scope_ref = str(scope_ref_value)
    row = conn.execute("SELECT id, box, correct_streak, practice_count FROM competency_progress "
                       "WHERE user_id = ? AND scope_kind = ? AND scope_ref = ? AND competency_key = ?",
                       (user_id, scope_kind, scope_ref, item["primary_competency"])).fetchone()
    box = row[1] if row else 0
    streak = row[2] if row else 0
    count = row[3] if row else 0
    if verdict == "meets":
        box, streak, due = min(4, box + 1), streak + 1, (date.fromisoformat(today) + timedelta(days=(2, 4, 7, 14, 30)[min(4, box + 1)])).isoformat()
        wrong = None
    elif verdict == "partially_meets":
        streak, due, wrong = 0, (date.fromisoformat(today) + timedelta(days=2)).isoformat(), today
    else:
        box, streak, due, wrong = 0, 0, today, today
    timestamp = now_iso()
    if row:
        conn.execute("UPDATE competency_progress SET box = ?, correct_streak = ?, practice_count = ?, "
                     "last_verdict = ?, last_asked_time = ?, last_wrong_time = COALESCE(?, last_wrong_time), "
                     "due_date = ?, updated_time = ? WHERE id = ?",
                     (box, streak, count + 1, verdict, timestamp, wrong, due, timestamp, row[0]))
    else:
        conn.execute("INSERT INTO competency_progress (user_id, scope_kind, scope_ref, context_label, "
                     "competency_key, box, correct_streak, practice_count, last_verdict, last_asked_time, "
                     "last_wrong_time, due_date, created_time, updated_time) VALUES (?, ?, ?, ?, ?, ?, ?, 1, "
                     "?, ?, ?, ?, ?, ?)",
                     (user_id, scope_kind, scope_ref, context[1], item["primary_competency"], box, streak,
                      verdict, timestamp, wrong, due, timestamp, timestamp))


def record_answer(db_path: str, user_id: str, session_id: int, session_item_id: int,
                  token: str, *, transcript: list, verdict: str, stuck: bool,
                  feedback: dict, today: str) -> bool:
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT plan_json FROM grill_sessions WHERE user_id = ? AND id = ? AND state = 'active'",
                           (user_id, session_id)).fetchone()
        item_row = conn.execute(
            "SELECT qi.primary_competency, qi.repeat_scope, qi.canonical_question_id FROM grill_session_items si "
            "JOIN question_set_items qi ON qi.id = si.question_set_item_id WHERE si.user_id = ? AND si.id = ? "
            "AND si.session_id = ? AND si.claim_token = ? AND si.state = 'unanswered'",
            (user_id, session_item_id, session_id, token),
        ).fetchone()
        if row is None or item_row is None:
            return False
        plan = loads_json(row[0], {})
        conn.execute("INSERT INTO grill_answers (user_id, session_id, session_item_id, question_id, "
                     "transcript_json, verdict, stuck, feedback, created_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (user_id, session_id, session_item_id, item_row[2], json.dumps(transcript, ensure_ascii=False),
                      verdict, int(stuck), json.dumps(feedback, ensure_ascii=False), timestamp))
        conn.execute("UPDATE grill_session_items SET state = 'answered', claim_token = NULL, "
                     "claim_started_time = NULL WHERE id = ?", (session_item_id,))
        _update_progress(conn, user_id=user_id, session_id=session_id,
                         item={"primary_competency": item_row[0], "repeat_scope": item_row[1]},
                         verdict=verdict, today=today)
        plan["current_ordinal"] = int(plan.get("current_ordinal", 0)) + 1
        plan["transcript"] = []
        plan["follow_up_used"] = False
        conn.execute("UPDATE grill_sessions SET plan_json = ?, updated_time = ? WHERE id = ?",
                     (json.dumps(plan, ensure_ascii=False), timestamp, session_id))
        return True


def record_skip(db_path: str, user_id: str, session_id: int, session_item_id: int,
                token: str) -> bool:
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT plan_json FROM grill_sessions WHERE user_id = ? AND id = ? AND state = 'active'",
                           (user_id, session_id)).fetchone()
        owned = conn.execute("SELECT qi.canonical_question_id FROM grill_session_items si "
                             "JOIN question_set_items qi ON qi.id = si.question_set_item_id "
                             "WHERE si.user_id = ? AND si.id = ? AND si.session_id = ? AND si.claim_token = ? "
                             "AND si.state = 'unanswered'", (user_id, session_item_id, session_id, token)).fetchone()
        if row is None or owned is None:
            return False
        conn.execute("INSERT INTO grill_answers (user_id, session_id, session_item_id, question_id, "
                     "transcript_json, verdict, stuck, feedback, created_time) VALUES (?, ?, ?, ?, '[]', "
                     "'skipped', 1, '{}', ?)", (user_id, session_id, session_item_id, owned[0], timestamp))
        conn.execute("UPDATE grill_session_items SET state = 'skipped', claim_token = NULL, "
                     "claim_started_time = NULL WHERE id = ?", (session_item_id,))
        plan = loads_json(row[0], {})
        plan.update(current_ordinal=int(plan.get("current_ordinal", 0)) + 1,
                    transcript=[], follow_up_used=False)
        conn.execute("UPDATE grill_sessions SET plan_json = ?, updated_time = ? WHERE id = ?",
                     (json.dumps(plan, ensure_ascii=False), timestamp, session_id))
        return True


def finish_if_complete(db_path: str, user_id: str, session_id: int) -> dict | None:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT plan_json, state FROM grill_sessions WHERE user_id = ? AND id = ?",
                           (user_id, session_id)).fetchone()
        if row is None:
            return None
        plan = loads_json(row[0], {})
        if int(plan.get("current_ordinal", 0)) < int(plan.get("total", 0)):
            return None
        counts = dict(conn.execute("SELECT verdict, COUNT(*) FROM grill_answers WHERE user_id = ? "
                                   "AND session_id = ? GROUP BY verdict", (user_id, session_id)).fetchall())
        summary = {"total": int(plan.get("total", 0)), "by_verdict": counts}
        timestamp = now_iso()
        conn.execute("UPDATE grill_sessions SET state = 'finished', summary_json = ?, ended_time = ?, "
                     "updated_time = ? WHERE user_id = ? AND id = ?",
                     (json.dumps(summary, ensure_ascii=False), timestamp, timestamp, user_id, session_id))
        return summary


def set_state(db_path: str, user_id: str, session_id: int, source: str, target: str) -> bool:
    with transaction(db_path) as conn:
        if target == "active" and conn.execute("SELECT 1 FROM grill_sessions WHERE user_id = ? "
                                                "AND state = 'active' AND id != ?", (user_id, session_id)).fetchone():
            raise ValueError("已有另一场进行中练习")
        cursor = conn.execute(
            "UPDATE grill_sessions SET state = ?, updated_time = ? WHERE user_id = ? "
            "AND id = ? AND state = ? AND NOT EXISTS (SELECT 1 FROM grill_session_items "
            "WHERE session_id = grill_sessions.id AND claim_token IS NOT NULL)",
            (target, now_iso(), user_id, session_id, source),
        )
        return cursor.rowcount == 1


def list_sessions(db_path: str, user_id: str, states: list[str]) -> list[dict]:
    valid = [state for state in states if state in {"active", "suspended", "finished"}]
    if not valid:
        return []
    marks = ",".join("?" for _ in valid)
    with read_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT s.id, s.question_set_id, s.kind, s.edition, s.context_label, s.state, "
            "COUNT(a.id), COUNT(si.id), s.started_time, s.ended_time FROM grill_sessions s "
            "LEFT JOIN grill_session_items si ON si.session_id = s.id "
            "LEFT JOIN grill_answers a ON a.session_item_id = si.id WHERE s.user_id = ? "
            f"AND s.state IN ({marks}) GROUP BY s.id ORDER BY s.id DESC", (user_id, *valid),
        ).fetchall()
    return [{"id": row[0], "question_set_id": row[1], "kind": row[2], "edition": row[3],
             "context_label": row[4], "state": row[5], "answered": row[6], "total": row[7],
             "started_time": row[8], "ended_time": row[9]} for row in rows]


def replay(db_path: str, user_id: str, session_id: int) -> dict | None:
    session = get_session(db_path, user_id, session_id)
    if session is None or session["state"] != "finished":
        return None
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT si.id, qi.text, qi.category, a.verdict, a.stuck, a.feedback, "
            "COALESCE(si.session_owned_guide_json, qi.answer_guide_json), qi.primary_competency, "
            "a.transcript_json FROM grill_answers a JOIN grill_session_items si ON si.id = a.session_item_id "
            "JOIN question_set_items qi ON qi.id = si.question_set_item_id WHERE a.user_id = ? "
            "AND a.session_id = ? ORDER BY si.ordinal", (user_id, session_id),
        ).fetchall()
    answers = [{"session_item_id": row[0], "text": row[1], "category": row[2], "verdict": row[3],
                "stuck": bool(row[4]), "feedback": loads_json(row[5], {}),
                "answer_guide": loads_json(row[6], {}), "primary_competency": row[7],
                "transcript": loads_json(row[8], [])} for row in rows]
    return {"status": "ok", "session_id": session_id, "context_label": session["context_label"],
            "kind": session["kind"], "edition": session["edition"], "answers": answers,
            "summary": session["summary"] or {}}


def delete_session(db_path: str, user_id: str, session_id: int) -> bool:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM grill_sessions WHERE user_id = ? AND id = ?",
                        (user_id, session_id)).fetchone() is None:
            return False
        conn.execute("DELETE FROM grill_answers WHERE user_id = ? AND session_id = ?", (user_id, session_id))
        conn.execute("DELETE FROM grill_session_items WHERE user_id = ? AND session_id = ?", (user_id, session_id))
        conn.execute("DELETE FROM grill_sessions WHERE user_id = ? AND id = ?", (user_id, session_id))
        return True


def grill_overview(db_path: str, user_id: str, *, context: str | None = None) -> dict:
    conditions = ["s.user_id = ?"]
    params: list = [user_id]
    if context:
        conditions.append("s.context_label LIKE ?")
        params.append(f"%{context}%")
    where = " AND ".join(conditions)
    with read_connection(db_path) as conn:
        states = dict(conn.execute(
            f"SELECT state, COUNT(*) FROM grill_sessions s WHERE {where} GROUP BY state", params,
        ).fetchall())
        verdicts = dict(conn.execute(
            f"SELECT a.verdict, COUNT(*) FROM grill_answers a JOIN grill_sessions s "
            f"ON s.id = a.session_id WHERE {where} GROUP BY a.verdict", params,
        ).fetchall())
    return {"total_sessions": sum(states.values()), "by_state": states,
            "total_answers": sum(verdicts.values()), "verdicts": verdicts}
