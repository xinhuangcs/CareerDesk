
import asyncio
from uuid import uuid4

import pytest

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.platform.database import connection as db
from careerdesk.features.applications.operations import (
    approve_application_delete_operation,
    prepare_application_delete_operation,
)
from careerdesk.features.applications.public import execute_application_update_operation
from careerdesk.features.journal.public import append_review
from tests.review_record_test_helpers import derive_review_for_test
from careerdesk.orchestration.maintenance.service import (
    MaintenanceService,
    upgrade_status,
)
from careerdesk.orchestration.maintenance import service as maintenance_module

EXTRACTION = {
    "company": "字节",
    "position": "LLM应用",
    "channel": None,
    "history": {
        "step": "二面",
        "date": "2026-07-07",
        "outcome": None,
        "summary": "字节二面完成",
    },
    "projected_state": {"stage": "interviewing", "current_step": "二面"},
    "next_action": {
        "stage": "interviewing",
        "step": "等待面试结果",
        "date": "2026-07-14",
        "time": None,
        "note": "一周内出结果",
    },
    "questions": [{"text": "检查点恢复怎么保证幂等？", "stuck": True,
                   "knowledge_points": ["检查点幂等"]}],
    "mood": "状态一般", "time_of_day": "afternoon", "factors": ["睡眠差"],
}


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "m.db")
    init_db(path)
    return path


def _record(db_path: str, user_id: str = "u1", extraction: dict | None = None) -> int:
    payload = extraction or EXTRACTION
    created = append_review(db_path, user_id, f"{payload['company']}复盘")
    derive_review_for_test(db_path, user_id, created["id"], payload)
    return created["id"]


def test_fresh_db_stamped_to_current_versions(db_path):
    assert db.get_meta(db_path, "derive_version") == str(db.DERIVE_VERSION)
    assert db.get_meta(db_path, "extract_version") == str(db.EXTRACT_VERSION)


def test_meta_roundtrip_overwrite_and_default(db_path):
    db.set_meta(db_path, "k", "v")
    assert db.get_meta(db_path, "k") == "v"
    db.set_meta(db_path, "k", 7)
    assert db.get_meta(db_path, "k") == "7"
    assert db.get_meta(db_path, "missing", "d") == "d"


def test_upgrade_status_none_pending_when_current(db_path):
    assert upgrade_status(db_path, "u1") == {
        "derive_pending": False, "pending_count": 0,
    }


def test_pending_count_only_includes_applied_reviews(db_path):
    append_review(db_path, "u1", "补问没完成")
    _record(db_path, "u1")
    db.set_meta(db_path, "derive_version", "0")

    status = upgrade_status(db_path, "u1")
    assert status == {"derive_pending": True, "pending_count": 1}


def test_safe_metadata_reconciliation_marks_version_without_replaying_projection(db_path):
    _record(db_path)
    with transaction(db_path) as conn:
        conn.execute("UPDATE applications SET next_note=NULL WHERE user_id='u1'")
    db.set_meta(db_path, "derive_version", "0")

    result = run(MaintenanceService(db_path).reconcile("u1"))
    assert result == {"status": "ok", "reconciled": 1}
    with read_connection(db_path) as conn:
        note = conn.execute(
            "SELECT next_note FROM applications WHERE user_id='u1'"
        ).fetchone()[0]
    assert note is None
    assert upgrade_status(db_path, "u1")["derive_pending"] is False


def test_missing_cache_is_never_replayed_into_business_tables(db_path):
    created = append_review(db_path, "u1", "历史成功但缓存损坏")
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET state='applied', processed_time=?, revision=1 WHERE id=?",
            (db.now_iso(), created["id"]),
        )
    db.set_meta(db_path, "derive_version", "0")

    assert upgrade_status(db_path, "u1")["pending_count"] == 1
    result = run(MaintenanceService(db_path).reconcile("u1"))
    assert result == {"status": "ok", "reconciled": 1}
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert upgrade_status(db_path, "u1")["derive_pending"] is False


def test_application_update_corrections_are_not_counted_or_replayed(db_path):
    with transaction(db_path) as conn:
        timestamp = db.now_iso()
        conn.execute(
            "INSERT INTO applications (user_id, company, position, stage, created_time, updated_time) "
            "VALUES ('u1', 'B', 'P2', 'withdrawn', ?, ?)",
            (timestamp, timestamp),
        )
    operation = execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company="B",
        position="P2",
        changes={"stage": "pooled"},
    )
    assert operation["state"] == "completed"
    db.set_meta(db_path, "derive_version", "0")

    assert upgrade_status(db_path, "u1")["pending_count"] == 0
    result = run(MaintenanceService(db_path).reconcile("u1"))
    assert result == {"status": "completed", "reconciled": 0}
    with read_connection(db_path) as conn:
        stage = conn.execute(
            "SELECT stage FROM applications WHERE user_id='u1' AND company='B' AND position='P2'"
        ).fetchone()[0]
    assert stage == "pooled"


def test_metadata_reconciliation_preserves_recreated_application_generation(db_path):
    first = _record(db_path)
    with read_connection(db_path) as conn:
        company, position = conn.execute(
            "SELECT company, position FROM applications"
        ).fetchone()
    proposal = prepare_application_delete_operation(
        db_path,
        "u1",
        company=company,
        position=position,
    )
    approve_application_delete_operation(db_path, "u1", proposal["operation_id"])
    second = _record(db_path)
    assert second > first
    with transaction(db_path) as conn:
        application_id = conn.execute("SELECT id FROM applications").fetchone()[0]
        resume_id = conn.execute(
            "INSERT INTO resumes (user_id, name, binding, application_id, content_text, "
            "content_hash, extraction_receipt_json, segments_json, created_time, updated_time) "
            "VALUES ('u1', '新生命周期专属', 'application', ?, 'resume', ?, '{}', '[]', "
            "'r0', 'r0')",
            (application_id, "0" * 64),
        ).lastrowid
        conn.execute(
            "UPDATE applications SET channel='内推', jd_text='新 JD', priority='high', "
            "resume_id=?, prep_status='ready', prep_json='{\"ready\":true}' WHERE id=?",
            (resume_id, application_id),
        )
    db.set_meta(db_path, "derive_version", "0")

    assert run(MaintenanceService(db_path).reconcile("u1")) == {
        "status": "ok", "reconciled": 2,
    }
    with read_connection(db_path) as conn:
        application = conn.execute(
            "SELECT id, channel, jd_text, priority, resume_id, prep_status, prep_json "
            "FROM applications"
        ).fetchone()
        resume = conn.execute(
            "SELECT application_id, binding FROM resumes WHERE id=?", (resume_id,)
        ).fetchone()
    assert application == (
        application_id, "内推", "新 JD", "high", resume_id, "ready", '{"ready":true}',
    )
    assert resume == (application_id, "application")


def test_upgrade_versions_are_marked_per_user_with_global_fallback(db_path):
    _record(db_path, "u1")
    _record(db_path, "u2", {**EXTRACTION, "company": "腾讯"})
    db.set_meta(db_path, "derive_version", "0")
    db.set_meta(db_path, "extract_version", "0")
    assert upgrade_status(db_path, "u1")["derive_pending"] is True
    assert upgrade_status(db_path, "u2")["derive_pending"] is True

    assert run(MaintenanceService(db_path).reconcile("u1"))["status"] == "ok"
    assert upgrade_status(db_path, "u1")["derive_pending"] is False
    assert upgrade_status(db_path, "u2")["derive_pending"] is True
    assert db.get_meta(db_path, "extract_version") == "0"


def test_never_applied_review_is_not_reconciled(db_path):
    append_review(db_path, "u1", "字节面试，补问未完成")
    db.set_meta(db_path, "derive_version", "0")

    result = run(MaintenanceService(db_path).reconcile("u1"))
    assert result == {"status": "completed", "reconciled": 0}
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_reconciliation_preserves_grill_earned_box(db_path):
    _record(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE knowledge_points SET box = 3, correct_streak = 2 "
            "WHERE user_id='u1' AND name='检查点幂等'"
        )
    db.set_meta(db_path, "derive_version", "0")

    result = run(MaintenanceService(db_path).reconcile("u1"))
    assert result["status"] == "ok"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT box FROM knowledge_points WHERE user_id='u1' AND name='检查点幂等'"
        ).fetchone()[0] == 3


def test_reconciliation_preserves_later_application_projection(db_path):
    _record(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET stage='offer', current_step='已收到 Offer', "
            "applied_date='2026-07-01', next_stage='offer', next_step='确认入职', "
            "next_date='2026-07-20', next_note=NULL WHERE user_id='u1'"
        )
    db.set_meta(db_path, "derive_version", "0")

    assert run(MaintenanceService(db_path).reconcile("u1"))["status"] == "ok"
    with read_connection(db_path) as conn:
        projection = conn.execute(
            "SELECT stage, current_step, applied_date, next_stage, next_step, next_date, "
            "next_note FROM applications"
        ).fetchone()
    assert projection == (
        "offer", "已收到 Offer", "2026-07-01", "offer", "确认入职",
        "2026-07-20", None,
    )


def test_reconciliation_never_backfills_a_user_cleared_next_action_note(db_path):
    _record(db_path)
    with transaction(db_path) as conn:
        conn.execute("UPDATE applications SET next_note=NULL WHERE user_id='u1'")
    db.set_meta(db_path, "derive_version", "0")

    assert run(MaintenanceService(db_path).reconcile("u1"))["status"] == "ok"
    with read_connection(db_path) as conn:
        next_action = conn.execute(
            "SELECT next_date, next_note FROM applications"
        ).fetchone()
    assert next_action == (EXTRACTION["next_action"]["date"], None)


def test_snapshot_change_aborts_without_version_stamp(db_path, monkeypatch):
    _record(db_path)
    db.set_meta(db_path, "derive_version", "0")
    original = maintenance_module.journal.snapshot_in_transaction

    def changed(conn, user_id):
        return (*original(conn, user_id), (999999, "applied", 1))

    monkeypatch.setattr(maintenance_module.journal, "snapshot_in_transaction", changed)
    result = run(MaintenanceService(db_path).reconcile("u1"))
    assert result["status"] == "error" and "变化" in result["message"]
    assert upgrade_status(db_path, "u1")["derive_pending"] is True


def test_concurrent_maintenance_requests_do_not_double_apply(db_path):
    _record(db_path)
    db.set_meta(db_path, "derive_version", "0")

    async def both():
        return await asyncio.gather(
            MaintenanceService(db_path).reconcile("u1"),
            MaintenanceService(db_path).reconcile("u1"),
        )

    results = run(both())
    assert {result["status"] for result in results} == {"ok", "completed"}
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE user_id='u1'",
        ).fetchone()[0] == 1
