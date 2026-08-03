"""Question catalogue exposes the two generated practice editions."""

from careerdesk.features.questions.repository import list_questions
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction


def _generated_question(conn, *, edition: str, label: str, text: str) -> int:
    timestamp = now_iso()
    set_id = conn.execute(
        "INSERT INTO question_sets (user_id, kind, edition, resume_id, application_id, "
        "state, stage, "
        "generation, material_fingerprint, policy_fingerprint, generation_fingerprint, "
        "prompt_version, schema_version, rubric_version, segmentation_version, "
        "summary_policy_version, model_label, input_receipt_json, coverage_json, context_label, "
        "created_time, updated_time) VALUES ('u1', 'generated', ?, 1, ?, 'ready', 'ready', "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?, ?, ?)",
        (edition, 1 if edition == "custom" else None,
         f"generation-{edition}", f"material-{edition}", f"policy-{edition}",
         f"fingerprint-{edition}", "prompt-v1", "schema-v1", "rubric-v1", "segments-v1",
         "summary-v1", "model", label, timestamp, timestamp),
    ).lastrowid
    return conn.execute(
        "INSERT INTO questions (user_id, text, source, question_set_id, created_time, updated_time) "
        "VALUES ('u1', ?, 'generated', ?, ?, ?)", (text, set_id, timestamp, timestamp),
    ).lastrowid


def test_catalogue_filters_generated_questions_by_practice_edition(tmp_path):
    db_path = str(tmp_path / "catalogue.db")
    init_db(db_path)
    with transaction(db_path) as conn:
        basic_id = _generated_question(
            conn, edition="basic", label="通用简历", text="请介绍一次协作经历",
        )
        _generated_question(
            conn, edition="custom", label="示例公司 · 产品经理", text="如何理解这个岗位",
        )
        conn.execute(
            "INSERT INTO questions (user_id, text, source, created_time, updated_time) "
            "VALUES ('u1', '复盘真题', 'real', ?, ?)", (now_iso(), now_iso()),
        )

    items = list_questions(db_path, "u1", edition="basic")

    assert [item["id"] for item in items] == [basic_id]
    assert items[0]["edition"] == "basic"
    assert items[0]["context_label"] == "通用简历"


def test_large_catalogue_browse_uses_user_status_ordering_index(tmp_path):
    db_path = str(tmp_path / "catalogue-plan.db")
    init_db(db_path)

    with read_connection(db_path) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT q.id FROM questions q "
            "LEFT JOIN question_sets qs "
            "ON qs.user_id = q.user_id AND qs.id = q.question_set_id "
            "WHERE q.user_id = ? AND q.status = 'active' AND qs.edition = ? "
            "ORDER BY q.created_time DESC, q.id DESC LIMIT ? OFFSET ?",
            ("u1", "basic", 101, 0),
        ).fetchall()

    detail = " ".join(row[3] for row in plan)
    assert "idx_questions_user_status_created" in detail
    assert "USE TEMP B-TREE FOR ORDER BY" not in detail
