"""Question catalogue reads plus immutable-revision-safe metadata actions."""

import hashlib
import json

from ...platform.database import loads_json, now_iso, read_connection, transaction


def set_quality_flag(db_path: str, user_id: str, question_id: int, flag: str | None) -> bool:
    if flag not in {"good", "bad", None}:
        raise ValueError("quality_flag 只能是 good/bad/None")
    with transaction(db_path) as conn:
        cursor = conn.execute("UPDATE questions SET quality_flag = ?, updated_time = ? "
                              "WHERE user_id = ? AND id = ? AND status = 'active'",
                              (flag, now_iso(), user_id, question_id))
        return cursor.rowcount == 1


def verify_answer_guide(db_path: str, user_id: str, question_id: int, *, verified: bool) -> bool:
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT answer_guide_json, immutable_revision FROM questions "
                           "WHERE user_id = ? AND id = ? AND status = 'active'",
                           (user_id, question_id)).fetchone()
        if row is None or not row[0]:
            return False
        receipt = None
        if verified:
            receipt = json.dumps({"version": "answer-verification-v1", "guide_hash": hashlib.sha256(
                row[0].encode("utf-8")).hexdigest(), "question_revision": row[1],
                "verified_time": now_iso()}, separators=(",", ":"))
        conn.execute("UPDATE questions SET answer_verification_json = ?, updated_time = ? "
                     "WHERE user_id = ? AND id = ?", (receipt, now_iso(), user_id, question_id))
        return True


def question_overview(db_path: str, user_id: str, *, company: str | None = None,
                      knowledge_point: str | None = None, source: str | None = None) -> dict:
    conditions, params = ["q.user_id = ?", "q.status = 'active'"], [user_id]
    if source:
        conditions.append("q.source = ?")
        params.append(source)
    if company:
        conditions.append(
            "((q.source = 'real' AND EXISTS ("
            "SELECT 1 FROM review_question_occurrences occurrence JOIN journal source "
            "ON source.user_id = occurrence.user_id AND source.id = occurrence.journal_id "
            "WHERE occurrence.user_id = q.user_id AND occurrence.question_id = q.id "
            "AND source.kind = 'review' AND source.state = 'applied' "
            "AND occurrence.company LIKE ?)) OR (q.source != 'real' AND "
            "COALESCE(q.company, '') LIKE ?))"
        )
        params.extend((f"%{company}%", f"%{company}%"))
    if knowledge_point:
        conditions.append("COALESCE(q.primary_competency, '') LIKE ?")
        params.append(f"%{knowledge_point}%")
    where = " AND ".join(conditions)
    with read_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT q.source, COUNT(*), SUM(CASE WHEN EXISTS (SELECT 1 FROM grill_answers a "
            "WHERE a.question_id = q.id) THEN 1 ELSE 0 END) FROM questions q "
            f"WHERE {where} GROUP BY q.source", params,
        ).fetchall()
    by_source = {row[0]: {"total": row[1], "practiced": row[2],
                           "unpracticed": row[1] - row[2]} for row in rows}
    return {"total": sum(v["total"] for v in by_source.values()),
            "practiced": sum(v["practiced"] for v in by_source.values()),
            "unpracticed": sum(v["unpracticed"] for v in by_source.values()),
            "by_source": by_source}


def competency_overview(db_path: str, user_id: str) -> dict:
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT competency_key, SUM(practice_count), MAX(last_asked_time), "
            "SUM(CASE last_verdict WHEN 'meets' THEN 2 WHEN 'partially_meets' THEN 1 ELSE 0 END), "
            "COUNT(*) FROM competency_progress WHERE user_id = ? GROUP BY competency_key "
            "ORDER BY MAX(last_asked_time) DESC, competency_key", (user_id,),
        ).fetchall()
        scopes = conn.execute(
            "SELECT scope_kind, scope_ref, context_label, competency_key, box, practice_count, "
            "last_verdict, due_date FROM competency_progress WHERE user_id = ? "
            "ORDER BY scope_kind, context_label, competency_key", (user_id,),
        ).fetchall()
    return {"aggregate": [{"competency": row[0], "practice_count": row[1],
                            "last_asked_time": row[2], "performance_points": row[3],
                            "scope_count": row[4]} for row in rows],
            "scopes": [{"scope_kind": row[0], "scope_ref": row[1], "context_label": row[2],
                        "competency": row[3], "box": row[4], "practice_count": row[5],
                        "last_verdict": row[6], "due_date": row[7]} for row in scopes]}


def knowledge_overview(db_path: str, user_id: str) -> dict:
    overview = competency_overview(db_path, user_id)
    by_box = {str(box): 0 for box in range(5)}
    for item in overview["scopes"]:
        by_box[str(item["box"])] += 1
    return {"total": len(overview["scopes"]), "due": sum(
        1 for item in overview["scopes"] if item["due_date"]), "by_box": by_box,
        "aggregate": overview["aggregate"], "scopes": overview["scopes"]}


def find_knowledge_points(db_path: str, user_id: str, name: str) -> list[dict]:
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT competency_key, MIN(box), SUM(practice_count), MAX(last_asked_time) "
            "FROM competency_progress WHERE user_id = ? AND competency_key LIKE ? "
            "GROUP BY competency_key ORDER BY MIN(box), competency_key LIMIT 20",
            (user_id, f"%{name}%"),
        ).fetchall()
    return [{"name": row[0], "box": row[1], "practice_count": row[2],
             "last_asked_time": row[3]} for row in rows]


def list_weak_points(db_path: str, user_id: str, limit: int = 10) -> list[dict]:
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT competency_key, MIN(box), SUM(practice_count), MAX(last_wrong_time) "
            "FROM competency_progress WHERE user_id = ? GROUP BY competency_key "
            "ORDER BY MIN(box), MAX(last_wrong_time) DESC LIMIT ?", (user_id, limit),
        ).fetchall()
    return [{"id": index + 1, "name": row[0], "topic": None, "box": row[1],
             "correct_streak": 0, "last_wrong_time": row[3], "question_count": row[2]}
            for index, row in enumerate(rows)]


def list_questions(db_path: str, user_id: str, *, company: str | None = None,
                   knowledge_point: str | None = None, source: str | None = None,
                   category: str | None = None, channel: str | None = None,
                   context_id: int | None = None, edition: str | None = None,
                   order: str = "newest",
                   limit: int = 100, offset: int = 0, **_ignored) -> list[dict]:
    conditions, params = ["q.user_id = ?", "q.status = 'active'"], [user_id]
    for field, value in (("source", source), ("category", category), ("channel", channel)):
        if value:
            conditions.append(f"q.{field} = ?")
            params.append(value)
    if company:
        conditions.append(
            "((q.source = 'real' AND EXISTS ("
            "SELECT 1 FROM review_question_occurrences occurrence JOIN journal source "
            "ON source.user_id = occurrence.user_id AND source.id = occurrence.journal_id "
            "WHERE occurrence.user_id = q.user_id AND occurrence.question_id = q.id "
            "AND source.kind = 'review' AND source.state = 'applied' "
            "AND occurrence.company LIKE ?)) OR (q.source != 'real' AND "
            "COALESCE(q.company, '') LIKE ?))"
        )
        params.extend((f"%{company}%", f"%{company}%"))
    if knowledge_point:
        conditions.append("COALESCE(q.primary_competency, '') LIKE ?")
        params.append(f"%{knowledge_point}%")
    if context_id is not None:
        conditions.append("q.question_set_id = ?")
        params.append(context_id)
    if edition is not None:
        conditions.extend(("q.source = 'generated'", "qs.edition = ?"))
        params.append(edition)
    ordering = "q.created_time DESC, q.id DESC" if order != "oldest" else "q.created_time, q.id"
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT q.id, q.text, q.source, q.company, q.source_step, q.asked_date, "
            "q.quality_flag, q.category, q.channel, q.response_format, "
            "q.primary_competency, q.secondary_tags_json, q.answer_guide_json, "
            "q.answer_verification_json, q.question_set_id, q.immutable_revision, "
            "qs.edition, qs.context_label "
            "FROM questions q LEFT JOIN question_sets qs "
            "ON qs.user_id = q.user_id AND qs.id = q.question_set_id "
            f"WHERE {' AND '.join(conditions)} ORDER BY {ordering} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [{"id": row[0], "text": row[1], "source": row[2], "company": row[3],
             "source_step": row[4], "asked_date": row[5], "quality_flag": row[6],
             "category": row[7] or "professional_domain", "channel": row[8] or "interview",
             "response_format": row[9] or "oral_text",
             "primary_competency": row[10] or "通用表达",
             "secondary_tags": loads_json(row[11], []), "answer_guide": loads_json(row[12], None),
             "answer_verified": loads_json(row[13], None) is not None, "question_set_id": row[14],
             "immutable_revision": row[15], "knowledge_points": ([{"name": row[10], "box": 0}]
                 if row[10] else []), "weakest_box": None, "edition": row[16],
             "context_label": row[17]} for row in rows]
