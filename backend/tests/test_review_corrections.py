"""Review timeline-entry correction and whole-Review undo regressions."""

from uuid import uuid4

from careerdesk.agentic.tools import ManageReviewTool
from careerdesk.features.applications.public import (
    execute_application_update_operation,
    timeline_entry_snapshot_fingerprint,
)
from careerdesk.features.journal.public import append_review, read_merged_corrections
from careerdesk.features.questions.repository import question_overview
from careerdesk.features.reviews.public import (
    approve_review_operation,
    edit_review_timeline_entry_from_timeline,
    execute_review_timeline_entry_edit_operation,
    prepare_review_undo_operation,
)
from tests.review_record_test_helpers import derive_review_for_test
from careerdesk.platform.database import init_db, read_connection


def _extraction(
    company: str,
    position: str,
    *,
    step: str,
    occurred_date: str,
    stage: str,
    questions: list[dict] | None = None,
    next_action: dict | None = None,
) -> dict:
    return {
        "company": company,
        "position": position,
        "history": {
            "step": step,
            "date": occurred_date,
            "outcome": None,
            "summary": f"{company} {position} {step}",
        },
        "projected_state": {"stage": stage, "current_step": step},
        "next_action": next_action,
        "questions": questions or [],
        "factors": [],
    }


def _record(db_path: str, extraction: dict) -> int:
    summary = extraction["history"]["summary"]
    journal_id = append_review(db_path, "u1", summary)["id"]
    derive_review_for_test(db_path, "u1", journal_id, extraction)
    return journal_id


def _undo(db_path: str, journal_id: int) -> dict:
    proposal = prepare_review_undo_operation(db_path, "u1", journal_id=journal_id)
    return approve_review_operation(
        db_path, "u1", proposal["operation_id"],
    )["result"]


def _edit(db_path: str, company: str | None, position: str | None, **changes) -> dict:
    return execute_review_timeline_entry_edit_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company=company,
        position=position,
        changes=changes,
    )


def test_edit_review_timeline_entry_updates_step_date_and_current_step(tmp_path):
    db_path = str(tmp_path / "edit.db")
    init_db(db_path)
    journal_id = _record(db_path, _extraction(
        "腾讯", "后端", step="三面", occurred_date="2026-07-01", stage="interviewing",
    ))

    result = _edit(
        db_path, "腾讯", "后端", step="二面", occurred_date="2026-07-02",
    )

    assert result["state"] == "completed"
    assert result["effect"]["changed_fields"] == ["step", "occurred_date"]
    with read_connection(db_path) as conn:
        entry = conn.execute(
            "SELECT step, occurred_date, to_stage, to_step FROM timeline_entries "
            "WHERE journal_id = ?",
            (journal_id,),
        ).fetchone()
        application = conn.execute(
            "SELECT stage, current_step FROM applications",
        ).fetchone()
    assert entry == ("二面", "2026-07-02", "interviewing", "二面")
    assert application == ("interviewing", "二面")


def test_direct_timeline_edit_audit_is_not_a_review_supplement(tmp_path):
    db_path = str(tmp_path / "audit-is-not-supplement.db")
    init_db(db_path)
    journal_id = _record(db_path, _extraction(
        "腾讯", "后端", step="一面", occurred_date="2026-07-01",
        stage="interviewing",
    ))
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT e.id, e.application_id, e.step, e.occurred_date, e.outcome, e.summary, "
            "e.from_stage, e.from_step, e.to_stage, e.to_step, e.source, e.created_time, "
            "a.revision FROM timeline_entries e JOIN applications a "
            "ON a.user_id=e.user_id AND a.id=e.application_id WHERE e.journal_id=?",
            (journal_id,),
        ).fetchone()
    fingerprint = timeline_entry_snapshot_fingerprint(
        created_time=row[11],
        step=row[2],
        occurred_date=row[3],
        outcome=row[4],
        summary=row[5],
        from_stage=row[6],
        from_step=row[7],
        to_stage=row[8],
        to_step=row[9],
        source=row[10],
    )

    edited = edit_review_timeline_entry_from_timeline(
        db_path,
        "u1",
        row[1],
        row[0],
        expected_revision=row[12],
        expected_fingerprint=fingerprint,
        step="二面",
        occurred_date=row[3],
        outcome=row[4],
        summary=row[5],
    )

    assert edited is not None and edited["step"] == "二面"
    assert read_merged_corrections(db_path, "u1", journal_id) == []
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT operation_id, json_extract(derivation_json, '$.operation.type') "
            "FROM journal WHERE kind='correction' AND parent_journal_id=?",
            (journal_id,),
        ).fetchall() == [(None, "review_timeline_entry_edit")]


def test_edit_old_review_preserves_later_stage_step_and_next_action(tmp_path):
    db_path = str(tmp_path / "later-state.db")
    init_db(db_path)
    old = _record(db_path, _extraction(
        "腾讯",
        "后端",
        step="一面",
        occurred_date="2026-07-01",
        stage="interviewing",
        next_action={
            "stage": "interviewing", "step": "二面", "date": "2026-07-05",
            "time": None, "note": "等待安排",
        },
    ))
    execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company="腾讯",
        position="后端",
        changes={
            "stage": "offer",
            "current_step": "Offer 沟通",
            "next_action": {
                "stage": "offer", "step": "回复 Offer", "date": "2026-07-20",
                "time": None, "note": "确认入职日期",
            },
        },
    )

    result = _edit(db_path, "腾讯", "后端", occurred_date="2026-07-02")

    assert result["target"]["journal_id"] == old
    with read_connection(db_path) as conn:
        projection = conn.execute(
            "SELECT stage, current_step, next_stage, next_step, next_date, next_note "
            "FROM applications",
        ).fetchone()
    assert projection == (
        "offer", "Offer 沟通", "offer", "回复 Offer", "2026-07-20", "确认入职日期",
    )


def test_other_application_state_write_does_not_block_timeline_edit(tmp_path):
    db_path = str(tmp_path / "other-app.db")
    init_db(db_path)
    target = _record(db_path, _extraction(
        "A", "P1", step="一面", occurred_date="2026-07-01", stage="interviewing",
    ))
    _record(db_path, _extraction(
        "B", "P2", step="一面", occurred_date="2026-07-02", stage="interviewing",
    ))
    execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company="B",
        position="P2",
        changes={"stage": "pooled"},
    )

    result = _edit(db_path, "A", "P1", step="技术一面")

    assert result["target"]["journal_id"] == target
    with read_connection(db_path) as conn:
        stages = dict(conn.execute(
            "SELECT company, stage FROM applications ORDER BY company",
        ).fetchall())
    assert stages == {"A": "interviewing", "B": "pooled"}


def test_whole_review_undo_removes_timeline_and_archives_only_orphan_question(tmp_path):
    db_path = str(tmp_path / "undo.db")
    init_db(db_path)
    keep = _record(db_path, _extraction(
        "腾讯",
        "后端",
        step="一面",
        occurred_date="2026-07-01",
        stage="interviewing",
        questions=[{"text": "共享题?", "knowledge_points": []}],
    ))
    duplicate = _record(db_path, _extraction(
        "字节",
        "前端",
        step="一面",
        occurred_date="2026-07-02",
        stage="interviewing",
        questions=[
            {"text": "独有题?", "knowledge_points": []},
            {"text": "共享题?", "knowledge_points": []},
        ],
    ))

    result = _undo(db_path, duplicate)

    assert result["removed"]["timeline_entries"] == 1
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE journal_id = ?", (duplicate,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE journal_id = ?", (keep,),
        ).fetchone() == (1,)
        statuses = dict(conn.execute(
            "SELECT text, status FROM questions WHERE text IN ('独有题?', '共享题?')",
        ).fetchall())
    assert statuses == {"共享题?": "active", "独有题?": "archived"}
    assert question_overview(db_path, "u1")["by_source"]["real"]["total"] == 1
    assert prepare_review_undo_operation(
        db_path, "u1", journal_id=duplicate,
    ) == {"status": "not_found"}


def test_manage_review_targets_company_and_leaves_other_review_untouched(tmp_path):
    db_path = str(tmp_path / "tool-target.db")
    init_db(db_path)
    _record(db_path, _extraction(
        "腾讯", "后端", step="一面", occurred_date="2026-07-01", stage="interviewing",
    ))
    latest = _record(db_path, _extraction(
        "字节", "前端", step="在线测评", occurred_date="2026-07-02", stage="written_test",
    ))

    response = ManageReviewTool(
        db_path, "u1", client_turn_id=uuid4(),
    ).run({"action": "undo", "company": "腾讯"})
    assert response.status == "success" and "待确认" in response.text
    approve_review_operation(db_path, "u1", response.data["operation_id"])

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE journal_id = ?", (latest,),
        ).fetchone() == (1,)


def test_manage_review_undo_request_fence_reuses_pending_without_target_drift(tmp_path):
    db_path = str(tmp_path / "fence.db")
    init_db(db_path)
    older = _record(db_path, _extraction(
        "A", "P", step="一面", occurred_date="2026-07-01", stage="interviewing",
    ))
    latest = _record(db_path, _extraction(
        "B", "P", step="一面", occurred_date="2026-07-02", stage="interviewing",
    ))
    tool = ManageReviewTool(db_path, "u1", client_turn_id=uuid4())

    first = tool.run({"action": "undo"})
    blocked = tool.run({"action": "undo"})
    replay = ManageReviewTool(
        db_path, "u1", client_turn_id=uuid4(),
    ).run({"action": "undo"})

    assert first.data["target"]["journal_id"] == latest
    assert replay.data["operation_id"] == first.data["operation_id"]
    assert blocked.status == "error"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT id, state FROM journal WHERE kind='review' ORDER BY id",
        ).fetchall() == [(older, "applied"), (latest, "applied")]


def test_manage_review_company_ambiguity_requires_position(tmp_path):
    db_path = str(tmp_path / "company-ambiguity.db")
    init_db(db_path)
    backend = _record(db_path, _extraction(
        "腾讯", "后端", step="已投递", occurred_date="2026-07-01", stage="applied",
    ))
    hybrid = _record(db_path, _extraction(
        "腾讯", "混元", step="已投递", occurred_date="2026-07-02", stage="applied",
    ))

    ambiguous = ManageReviewTool(
        db_path, "u1", client_turn_id=uuid4(),
    ).run({"action": "edit_timeline_entry", "company": "腾讯", "new_step": "一面"})
    edited = ManageReviewTool(
        db_path, "u1", client_turn_id=uuid4(),
    ).run({
        "action": "edit_timeline_entry",
        "company": "腾讯",
        "position": "混元",
        "new_step": "一面",
    })

    assert ambiguous.status == "partial"
    assert "后端" in ambiguous.text and "混元" in ambiguous.text
    assert edited.status == "success"
    with read_connection(db_path) as conn:
        rows = dict(conn.execute(
            "SELECT journal_id, step FROM timeline_entries WHERE journal_id IN (?, ?)",
            (backend, hybrid),
        ).fetchall())
    assert rows == {backend: "已投递", hybrid: "一面"}


def test_manage_review_position_only_ambiguity_never_guesses_company(tmp_path):
    db_path = str(tmp_path / "position-ambiguity.db")
    init_db(db_path)
    first = _record(db_path, _extraction(
        "甲公司", "后端", step="已投递", occurred_date="2026-07-01", stage="applied",
    ))
    second = _record(db_path, _extraction(
        "乙公司", "后端", step="已投递", occurred_date="2026-07-02", stage="applied",
    ))

    response = ManageReviewTool(
        db_path, "u1", client_turn_id=uuid4(),
    ).run({"action": "edit_timeline_entry", "position": "后端", "new_step": "二面"})

    assert response.status == "partial"
    assert "甲公司·后端" in response.text and "乙公司·后端" in response.text
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT journal_id, step FROM timeline_entries "
            "WHERE journal_id IN (?, ?) ORDER BY journal_id",
            (first, second),
        ).fetchall()
    assert rows == [(first, "已投递"), (second, "已投递")]
