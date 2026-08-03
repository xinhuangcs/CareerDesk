"""Whole-Review undo: frozen previews, exact projection rollback, and HTTP safety."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.features.applications import operations as application_operations
from careerdesk.features.applications.public import apply_application_progress_in_transaction
from careerdesk.features.journal.public import append_review
from careerdesk.features.reviews.operations import undo as operations
from careerdesk.features.reviews.public import execute_review_timeline_entry_edit_operation
from tests.review_record_test_helpers import derive_review_for_test
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction


def _record(
    db_path: str,
    *,
    user_id: str = "u1",
    company: str = "腾讯",
    position: str = "后端",
    history_step: str | None = "一面",
    occurred_date: str | None = "2026-07-01",
    outcome: str | None = None,
    summary: str | None = None,
    projected_stage: str | None = "interviewing",
    current_step: str | None = "一面",
    next_stage: str | None = None,
    next_step: str | None = None,
    next_date: str | None = None,
    next_time: str | None = None,
    next_note: str | None = None,
    channel: str | None = None,
    question: str | None = None,
    with_status_log: bool = False,
) -> int:
    """Run the real journal + Review derivation path and return its journal id."""
    content = summary or f"{company}{position}{history_step or '进展'}"
    created = append_review(db_path, user_id, content)
    history = {
        "step": history_step,
        "date": occurred_date,
        "outcome": outcome,
        "summary": content,
    }
    projected_state = None
    if projected_stage is not None or current_step is not None:
        projected_state = {"stage": projected_stage, "current_step": current_step}
    next_action = None
    if next_step is not None:
        next_action = {
            "stage": next_stage or projected_stage or "interviewing",
            "step": next_step,
            "date": next_date,
            "time": next_time,
            "note": next_note,
        }
    extraction = {
        "company": company,
        "position": position,
        "channel": channel,
        "history": history,
        "projected_state": projected_state,
        "next_action": next_action,
        "questions": (
            [{"text": question, "stuck": False, "knowledge_points": []}]
            if question
            else []
        ),
        "mood": "紧张" if with_status_log else None,
        "time_of_day": "morning" if with_status_log else None,
        "factors": ["睡眠不足"] if with_status_log else [],
    }
    derive_review_for_test(
        db_path,
        user_id,
        created["id"],
        extraction,
        expected_revision=created["revision"],
    )
    return created["id"]


def _delete_application(db_path: str, user_id: str, company: str, position: str) -> dict:
    proposal = application_operations.prepare_application_delete_operation(
        db_path,
        user_id,
        company=company,
        position=position,
    )
    return application_operations.approve_application_delete_operation(
        db_path,
        user_id,
        proposal["operation_id"],
    )


def _edit_timeline_entry(
    db_path: str,
    user_id: str,
    company: str,
    position: str,
    **changes,
) -> dict:
    return execute_review_timeline_entry_edit_operation(
        db_path,
        user_id,
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company=company,
        position=position,
        changes=changes,
    )


def _timeline_entry_id(db_path: str, journal_id: int) -> int:
    with read_connection(db_path) as conn:
        return conn.execute(
            "SELECT id FROM timeline_entries WHERE journal_id=?",
            (journal_id,),
        ).fetchone()[0]


def _application_projection(
    db_path: str,
    *,
    user_id: str = "u1",
    company: str = "腾讯",
    position: str = "后端",
) -> dict:
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, stage, current_step, current_state_entry_id, next_stage, next_step, "
            "next_date, next_time, next_note, paused_from_stage, pause_reason, channel, "
            "applied_date, application_note, revision FROM applications "
            "WHERE user_id=? AND company=? AND position=?",
            (user_id, company, position),
        ).fetchone()
    keys = (
        "id", "stage", "current_step", "current_state_entry_id", "next_stage",
        "next_step", "next_date", "next_time", "next_note", "paused_from_stage",
        "pause_reason", "channel", "applied_date", "application_note", "revision",
    )
    return dict(zip(keys, row, strict=True))


def _business_snapshot(db_path: str, user_id: str = "u1") -> dict:
    """Exclude operation envelopes; compare only Review-owned business projections."""
    with read_connection(db_path) as conn:
        queries = {
            "reviews": (
                "SELECT id, state, revision, extraction_json, derivation_json FROM journal "
                "WHERE user_id=? AND kind='review' ORDER BY id"
            ),
            "applications": (
                "SELECT id, company, position, stage, current_step, current_state_entry_id, "
                "next_stage, next_step, next_date, next_time, next_note, paused_from_stage, "
                "pause_reason, channel, applied_date, revision FROM applications "
                "WHERE user_id=? ORDER BY id"
            ),
            "timeline_entries": (
                "SELECT id, application_id, step, occurred_date, outcome, summary, from_stage, "
                "from_step, to_stage, to_step, source, journal_id FROM timeline_entries "
                "WHERE user_id=? ORDER BY id"
            ),
            "status_logs": (
                "SELECT id, log_date, mood, journal_id FROM status_log "
                "WHERE user_id=? ORDER BY id"
            ),
            "questions": (
                "SELECT id, text, status, application_id FROM questions "
                "WHERE user_id=? ORDER BY id"
            ),
            "occurrences": (
                "SELECT journal_id, question_id, application_id, company, source_step, asked_date "
                "FROM review_question_occurrences WHERE user_id=? "
                "ORDER BY journal_id, question_id"
            ),
        }
        return {
            name: [tuple(row) for row in conn.execute(sql, (user_id,)).fetchall()]
            for name, sql in queries.items()
        }


def _journal_state(db_path: str, journal_id: int) -> tuple[str, int]:
    with read_connection(db_path) as conn:
        return conn.execute(
            "SELECT state, revision FROM journal WHERE id=?",
            (journal_id,),
        ).fetchone()


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "review-operations.db")
    init_db(path)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client, str(tmp_path / "careerdesk.db")
    get_settings.cache_clear()


def test_undo_locates_review_across_internal_space(db_path):
    _record(db_path, company="字节", position="AI应用工程师")
    proposal = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        company="字节",
        position="AI 应用工程师",
    )
    assert proposal["state"] == "pending"


def test_prepare_is_business_noop_and_reuses_same_live_target(db_path):
    target = _record(
        db_path,
        summary="腾讯后端一面",
        question="如何排查慢查询？",
        with_status_log=True,
    )
    before = _business_snapshot(db_path)
    proposal_links: list[tuple[str, str]] = []

    def record_proposal(conn, surface: str, operation_id: str) -> None:
        assert conn.in_transaction
        proposal_links.append((surface, operation_id))

    first = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        company="腾讯",
        position="后端",
        proposal_recorder=record_proposal,
    )
    second = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        company="腾讯",
        position="后端",
        proposal_recorder=record_proposal,
    )

    assert first == second
    assert proposal_links == [
        ("review_undo", first["operation_id"]),
        ("review_undo", first["operation_id"]),
    ]
    assert first["state"] == "pending"
    assert first["target"]["journal_id"] == target
    assert str(UUID(first["operation_id"])) == first["operation_id"]
    assert _business_snapshot(db_path) == before
    assert operations.list_pending_review_operations(db_path, "u1") == [first]
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id IS NOT NULL"
        ).fetchone() == (1,)


def test_approval_uses_frozen_target_even_after_a_newer_review_arrives(db_path):
    older = _record(db_path, company="A", position="P", summary="A 较早")
    target = _record(db_path, company="B", position="P", summary="B 目标")
    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    assert proposal["target"]["journal_id"] == target

    newer = _record(db_path, company="C", position="P", summary="C 更晚")
    completed = operations.approve_review_operation(
        db_path,
        "u1",
        proposal["operation_id"],
    )

    assert completed["state"] == "completed"
    assert completed["target"]["journal_id"] == target
    assert _journal_state(db_path, older)[0] == "applied"
    assert _journal_state(db_path, target)[0] == "voided"
    assert _journal_state(db_path, newer)[0] == "applied"


def test_prepare_latest_target_uses_append_order_not_mutable_timestamp(db_path):
    latest_by_time = _record(db_path, company="A", position="P", summary="时间更新")
    latest_by_id = _record(db_path, company="B", position="P", summary="ID 更新")
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET created_time='2026-07-03T00:00:00+00:00' WHERE id=?",
            (latest_by_time,),
        )
        conn.execute(
            "UPDATE journal SET created_time='2026-07-02T00:00:00+00:00' WHERE id=?",
            (latest_by_id,),
        )

    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    assert proposal["target"]["journal_id"] == latest_by_id


def test_schema_rejects_corrupt_identity_before_review_target_resolution(db_path):
    valid = _record(db_path, company="A", position="P", summary="有效身份")
    corrupt = _record(db_path, company="B", position="P", summary="损坏身份")
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "SELECT application_id FROM timeline_entries WHERE user_id='u1' AND journal_id=?",
            (corrupt,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE applications SET company='   ' WHERE user_id='u1' AND id=?",
                (application_id,),
            )

    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    assert proposal["target"]["journal_id"] == corrupt
    assert _journal_state(db_path, valid)[0] == "applied"
    assert _journal_state(db_path, corrupt)[0] == "applied"


def test_target_revision_drift_becomes_stale_without_partial_undo(db_path):
    target = _record(db_path, summary="原始一面")
    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    assert _edit_timeline_entry(
        db_path,
        "u1",
        "腾讯",
        "后端",
        step="二面",
    )["state"] == "completed"
    after_edit = _business_snapshot(db_path)

    with pytest.raises(operations.ReviewOperationConflict):
        operations.approve_review_operation(db_path, "u1", proposal["operation_id"])

    assert _business_snapshot(db_path) == after_edit
    assert _journal_state(db_path, target)[0] == "applied"
    assert operations.get_review_operation(
        db_path,
        "u1",
        proposal["operation_id"],
    )["state"] == "stale"


def test_dependency_drift_becomes_stale_without_partial_undo(db_path):
    target = _record(db_path, summary="要撤销的一面")
    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE timeline_entries SET summary='并发修正后的内容' "
            "WHERE user_id='u1' AND journal_id=?",
            (target,),
        )
    drifted = _business_snapshot(db_path)

    with pytest.raises(operations.ReviewOperationConflict):
        operations.approve_review_operation(db_path, "u1", proposal["operation_id"])

    assert _business_snapshot(db_path) == drifted
    assert _journal_state(db_path, target)[0] == "applied"
    assert operations.get_review_operation(
        db_path,
        "u1",
        proposal["operation_id"],
    )["state"] == "stale"


def test_approve_and_reject_are_idempotent_but_opposite_commands_conflict(db_path):
    approved_target = _record(db_path, company="A", position="P", summary="批准撤销")
    approved = operations.prepare_review_undo_operation(db_path, "u1")
    first = operations.approve_review_operation(db_path, "u1", approved["operation_id"])
    replay = operations.approve_review_operation(db_path, "u1", approved["operation_id"])
    assert first == replay and first["state"] == "completed"
    assert _journal_state(db_path, approved_target)[0] == "voided"
    with pytest.raises(operations.ReviewOperationConflict):
        operations.reject_review_operation(db_path, "u1", approved["operation_id"])

    retained_target = _record(db_path, company="B", position="P", summary="拒绝撤销")
    rejected = operations.prepare_review_undo_operation(db_path, "u1")
    first_reject = operations.reject_review_operation(db_path, "u1", rejected["operation_id"])
    replay_reject = operations.reject_review_operation(db_path, "u1", rejected["operation_id"])
    assert first_reject == replay_reject and first_reject["state"] == "rejected"
    assert _journal_state(db_path, retained_target)[0] == "applied"
    with pytest.raises(operations.ReviewOperationConflict):
        operations.approve_review_operation(db_path, "u1", rejected["operation_id"])


def test_operation_identity_is_tenant_scoped(db_path):
    target = _record(db_path, user_id="u1")
    proposal = operations.prepare_review_undo_operation(db_path, "u1")

    assert operations.get_review_operation(db_path, "u2", proposal["operation_id"]) is None
    with pytest.raises(operations.ReviewOperationNotFound):
        operations.approve_review_operation(db_path, "u2", proposal["operation_id"])
    with pytest.raises(operations.ReviewOperationNotFound):
        operations.reject_review_operation(db_path, "u2", proposal["operation_id"])
    assert _journal_state(db_path, target)[0] == "applied"


def test_review_endpoint_cannot_claim_another_operation_type(db_path):
    operation_id = "44444444-4444-4444-8444-444444444444"
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, operation_id) VALUES "
            "('u1', 'correction', '其它操作', '2026-07-01T00:00:00+00:00', '{}', ?, "
            "'awaiting_user', ?)",
            (json.dumps({"operation": {"type": "application_delete"}}), operation_id),
        )

    assert operations.get_review_operation(db_path, "u1", operation_id) is None
    with pytest.raises(operations.ReviewOperationNotFound):
        operations.approve_review_operation(db_path, "u1", operation_id)
    with pytest.raises(operations.ReviewOperationNotFound):
        operations.reject_review_operation(db_path, "u1", operation_id)


def test_corrupt_persisted_proposal_is_staled_without_business_writes(db_path):
    target = _record(db_path, question="只属于目标的题？", with_status_log=True)
    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    before = _business_snapshot(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET extraction_json='{}' WHERE operation_id=?",
            (proposal["operation_id"],),
        )

    with pytest.raises(operations.ReviewOperationConflict):
        operations.approve_review_operation(db_path, "u1", proposal["operation_id"])

    assert _business_snapshot(db_path) == before
    assert _journal_state(db_path, target)[0] == "applied"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT state FROM journal WHERE operation_id=?",
            (proposal["operation_id"],),
        ).fetchone() == ("superseded",)


@pytest.mark.parametrize("broken_timestamp", ["", "   "])
def test_corrupt_operation_timestamp_is_readable_then_safely_staled(
    db_path,
    broken_timestamp,
):
    target = _record(db_path, summary="时间戳损坏但目标不能误撤销")
    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET created_time=? WHERE operation_id=?",
            (broken_timestamp, proposal["operation_id"]),
        )

    corrupted = operations.get_review_operation(db_path, "u1", proposal["operation_id"])
    assert corrupted["state"] == "stale"
    assert corrupted["created_time"] == "1970-01-01T00:00:00+00:00"
    assert operations.list_pending_review_operations(db_path, "u1") == []
    with pytest.raises(operations.ReviewOperationConflict):
        operations.approve_review_operation(db_path, "u1", proposal["operation_id"])
    assert _journal_state(db_path, target)[0] == "applied"


def test_receipt_failure_rolls_back_target_and_every_projection(db_path):
    target = _record(
        db_path,
        question="事务必须保留的题？",
        with_status_log=True,
        next_stage="interviewing",
        next_step="二面",
        next_date="2026-07-10",
        next_note="等二面",
    )
    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    before = _business_snapshot(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_review_operation_receipt "
            "BEFORE UPDATE OF state ON journal "
            "WHEN OLD.operation_id IS NOT NULL AND NEW.state='applied' "
            "BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="receipt unavailable"):
        operations.approve_review_operation(db_path, "u1", proposal["operation_id"])

    assert _business_snapshot(db_path) == before
    assert _journal_state(db_path, target)[0] == "applied"
    assert operations.get_review_operation(
        db_path,
        "u1",
        proposal["operation_id"],
    )["state"] == "pending"


def test_undo_restores_stage_step_state_entry_and_next_action_exactly(db_path):
    first = _record(
        db_path,
        history_step="完成投递",
        occurred_date="2026-07-01",
        summary="已投腾讯后端",
        projected_stage="applied",
        current_step="完成投递",
        next_stage="interviewing",
        next_step="一面",
        next_date="2026-07-05",
        next_time="10:00",
        next_note="准备系统设计",
    )
    before = _application_projection(db_path)
    assert before["current_state_entry_id"] == _timeline_entry_id(db_path, first)

    target = _record(
        db_path,
        history_step="一面",
        occurred_date="2026-07-05",
        outcome="passed",
        summary="一面通过",
        projected_stage="interviewing",
        current_step="一面完成",
        next_stage="interviewing",
        next_step="二面",
        next_date="2026-07-10",
        next_time="14:30",
        next_note="等待二面",
        channel="内推",
    )
    target_entry = _timeline_entry_id(db_path, target)
    after_review = _application_projection(db_path)
    assert after_review["current_state_entry_id"] == target_entry

    # Personal notes are independent of Review and must survive the Review rollback.
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET application_note='保留用户备注', "
            "revision=revision+1, updated_time=? WHERE user_id='u1' AND id=?",
            (now_iso(), after_review["id"]),
        )
    live_before_prepare = _application_projection(db_path)
    proposal = operations.prepare_review_undo_operation(db_path, "u1", journal_id=target)
    expected = proposal["effect"]["application"]["expected"]
    replacement = proposal["effect"]["application"]["replacement"]
    assert expected["current_state_entry_id"] == target_entry
    assert replacement["stage"] == before["stage"]
    assert replacement["current_step"] == before["current_step"]
    assert replacement["current_state_entry_id"] == before["current_state_entry_id"]
    assert replacement["next_action"] == {
        "stage": "interviewing",
        "step": "一面",
        "date": "2026-07-05",
        "time": "10:00",
        "note": "准备系统设计",
    }

    completed = operations.approve_review_operation(db_path, "u1", proposal["operation_id"])
    restored = _application_projection(db_path)
    for field in (
        "stage", "current_step", "current_state_entry_id", "next_stage", "next_step",
        "next_date", "next_time", "next_note", "paused_from_stage", "pause_reason",
        "applied_date",
    ):
        assert restored[field] == before[field]
    assert restored["channel"] == before["channel"]
    assert restored["application_note"] == "保留用户备注"
    assert restored["revision"] == live_before_prepare["revision"] + 1
    assert completed["result"]["application_stage"] == "applied"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE journal_id=?",
            (target,),
        ).fetchone() == (0,)


def test_undo_only_review_progress_removes_application_created_by_that_review(db_path):
    target = _record(
        db_path,
        history_step="确认 Offer",
        occurred_date="2026-07-17",
        outcome="passed",
        summary="DTU TA Offer",
        projected_stage="offer",
        current_step="Offer 已确认",
    )
    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    application = proposal["effect"]["application"]
    assert application["record_exists"] is True
    assert application["record_retained"] is False

    completed = operations.approve_review_operation(db_path, "u1", proposal["operation_id"])
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM applications WHERE user_id='u1'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE user_id='u1'"
        ).fetchone() == (0,)
    assert _journal_state(db_path, target)[0] == "voided"
    assert completed["result"]["application_id"] is None
    assert completed["result"]["application_stage"] is None


def test_undo_earlier_creation_review_replays_later_review_and_retains_application(db_path):
    created = _record(
        db_path,
        history_step="完成投递",
        projected_stage="applied",
        current_step="完成投递",
        summary="投递腾讯后端",
    )
    later = _record(
        db_path,
        history_step="一面",
        occurred_date="2026-07-10",
        projected_stage="interviewing",
        current_step="一面完成",
        summary="完成腾讯后端一面",
    )
    later_entry = _timeline_entry_id(db_path, later)
    before = _application_projection(db_path)

    proposal = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        journal_id=created,
    )
    assert proposal["effect"]["application"]["record_retained"] is True
    assert proposal["effect"]["application"]["replacement"]["stage"] == "interviewing"
    assert proposal["effect"]["application"]["replacement"]["current_step"] == "一面完成"
    assert proposal["effect"]["application"]["replacement"][
        "current_state_entry_id"
    ] == later_entry

    completed = operations.approve_review_operation(
        db_path,
        "u1",
        proposal["operation_id"],
    )

    assert completed["state"] == "completed"
    assert _journal_state(db_path, created)[0] == "voided"
    assert _journal_state(db_path, later)[0] == "applied"
    restored = _application_projection(db_path)
    assert restored["id"] == before["id"]
    assert restored["stage"] == "interviewing"
    assert restored["current_step"] == "一面完成"
    assert restored["current_state_entry_id"] == later_entry
    assert restored["revision"] == before["revision"] + 1
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT journal_id FROM timeline_entries WHERE application_id=? ORDER BY id",
            (restored["id"],),
        ).fetchall() == [(later,)]


def test_undo_replay_does_not_infer_date_from_later_same_applied_stage_review(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, stage, current_step, created_time, updated_time) "
            "VALUES ('u1', '腾讯', '后端', 'applied', '已提交', ?, ?)",
            (now_iso(), now_iso()),
        )
    target = _record(
        db_path,
        history_step="复盘申请材料",
        occurred_date="2026-07-08",
        projected_stage="applied",
        current_step="材料复盘",
    )
    later = _record(
        db_path,
        history_step="确认投递状态",
        occurred_date="2026-07-10",
        projected_stage="applied",
        current_step="已确认",
    )

    proposal = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        journal_id=target,
    )

    assert proposal["effect"]["application"]["replacement"]["applied_date"] is None
    completed = operations.approve_review_operation(
        db_path,
        "u1",
        proposal["operation_id"],
    )
    assert completed["state"] == "completed"
    assert _journal_state(db_path, later)[0] == "applied"
    assert _application_projection(db_path)["applied_date"] is None


def test_profile_rename_keeps_review_undo_bound_to_stable_application(db_path):
    first = _record(
        db_path,
        history_step="完成投递",
        projected_stage="applied",
        current_step="完成投递",
        summary="旧公司旧岗位投递",
    )
    target = _record(
        db_path,
        history_step="一面",
        projected_stage="interviewing",
        current_step="一面完成",
        summary="旧公司旧岗位一面",
    )
    application_id = _application_projection(db_path)["id"]
    renamed = application_operations.execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company="腾讯",
        position="后端",
        changes={"company": "腾讯科技", "position": "高级后端"},
    )
    assert renamed["state"] == "completed"

    proposal = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        journal_id=target,
    )
    assert proposal["target"]["company"] == "腾讯科技"
    assert proposal["target"]["position"] == "高级后端"
    operations.approve_review_operation(db_path, "u1", proposal["operation_id"])

    restored = _application_projection(
        db_path,
        company="腾讯科技",
        position="高级后端",
    )
    assert restored["id"] == application_id
    assert restored["stage"] == "applied"
    assert restored["current_step"] == "完成投递"
    assert restored["current_state_entry_id"] == _timeline_entry_id(db_path, first)


def test_approved_merge_keeps_source_review_whole_undo_available(db_path):
    target = _record(
        db_path,
        company="Source",
        position="P1",
        summary="Source P1 一面",
        question="合并后仍可撤销的题？",
    )
    source_application_id = _application_projection(
        db_path,
        company="Source",
        position="P1",
    )["id"]
    timestamp = now_iso()
    with transaction(db_path) as conn:
        destination_application_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'Destination', 'P2', ?, ?)",
            (timestamp, timestamp),
        ).lastrowid
    merge = application_operations.prepare_application_merge_operation(
        db_path,
        "u1",
        source_application_id=source_application_id,
        source_company="Source",
        source_position="P1",
        destination_application_id=destination_application_id,
        destination_company="Destination",
        destination_position="P2",
    )
    assert application_operations.approve_application_merge_operation(
        db_path,
        "u1",
        merge["operation_id"],
    )["state"] == "completed"

    proposal = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        journal_id=target,
    )
    application = proposal["effect"]["application"]
    assert application["id"] == destination_application_id
    assert application["record_retained"] is True
    completed = operations.approve_review_operation(
        db_path,
        "u1",
        proposal["operation_id"],
    )

    assert completed["state"] == "completed"
    assert completed["result"]["application_id"] == destination_application_id
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT company, position FROM applications WHERE id=?",
            (destination_application_id,),
        ).fetchone() == ("Destination", "P2")
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE journal_id=?",
            (target,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT application_id, company FROM review_question_occurrences "
            "WHERE journal_id=?",
            (target,),
        ).fetchone() == (destination_application_id, "Destination")


def test_deleted_application_makes_review_undo_fail_closed(db_path):
    target = _record(
        db_path,
        summary="随后删除岗位的复盘",
        question="删除岗位后仍保留的题？",
        with_status_log=True,
    )
    assert _delete_application(db_path, "u1", "腾讯", "后端")["state"] == "completed"

    with pytest.raises(operations.ReviewOperationConflict):
        operations.prepare_review_undo_operation(
            db_path,
            "u1",
            company="腾讯",
            position="后端",
        )
    with pytest.raises(operations.ReviewOperationConflict):
        operations.prepare_review_undo_operation(db_path, "u1", journal_id=target)
    assert _journal_state(db_path, target)[0] == "applied"


def test_deleted_application_id_reuse_cannot_rebind_old_review(db_path):
    target = _record(db_path, company="旧公司", position="旧岗位", summary="旧岗位复盘")
    old_application_id = _application_projection(
        db_path,
        company="旧公司",
        position="旧岗位",
    )["id"]
    assert _delete_application(db_path, "u1", "旧公司", "旧岗位")["state"] == "completed"

    _record(db_path, company="新公司", position="新岗位", current_step="三面")
    new_application = _application_projection(
        db_path,
        company="新公司",
        position="新岗位",
    )
    assert new_application["id"] > old_application_id

    with pytest.raises(operations.ReviewOperationConflict):
        operations.prepare_review_undo_operation(db_path, "u1", journal_id=target)
    assert _application_projection(
        db_path,
        company="新公司",
        position="新岗位",
    ) == new_application


def test_cross_tenant_application_fk_is_never_treated_as_writable_binding(db_path):
    target = _record(db_path, company="旧公司", position="旧岗位", summary="旧岗位复盘")
    _record(
        db_path,
        user_id="u2",
        company="其它租户公司",
        position="其它岗位",
        current_step="四面",
    )
    foreign = _application_projection(
        db_path,
        user_id="u2",
        company="其它租户公司",
        position="其它岗位",
    )
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE timeline_entries SET application_id=? "
            "WHERE user_id='u1' AND journal_id=?",
            (foreign["id"], target),
        )

    with pytest.raises(operations.ReviewOperationConflict):
        operations.prepare_review_undo_operation(db_path, "u1", journal_id=target)
    assert _application_projection(
        db_path,
        user_id="u2",
        company="其它租户公司",
        position="其它岗位",
    ) == foreign


def test_undo_restores_snapshot_without_recomputing_from_unrelated_history(db_path):
    first = _record(
        db_path,
        history_step="完成投递",
        projected_stage="applied",
        current_step="完成投递",
        next_stage="interviewing",
        next_step="一面",
    )
    first_projection = _application_projection(db_path)
    target = _record(
        db_path,
        history_step="一面",
        projected_stage="interviewing",
        current_step="一面完成",
        next_stage="interviewing",
        next_step="二面",
    )
    target_entry = _timeline_entry_id(db_path, target)
    current = _application_projection(db_path)
    with transaction(db_path) as conn:
        manual = apply_application_progress_in_transaction(
            conn,
            "u1",
            current["id"],
            expected_revision=current["revision"],
            step="HR 补充沟通",
            occurred_date="2026-07-02",
            outcome=None,
            summary="补充了到岗时间",
            update_current_state=False,
            target_stage=None,
            target_step=None,
            source="manual",
        )
    assert manual is not None

    proposal = operations.prepare_review_undo_operation(db_path, "u1", journal_id=target)
    operations.approve_review_operation(db_path, "u1", proposal["operation_id"])
    restored = _application_projection(db_path)
    assert restored["stage"] == first_projection["stage"]
    assert restored["current_step"] == first_projection["current_step"]
    assert restored["current_state_entry_id"] == _timeline_entry_id(db_path, first)
    assert restored["current_state_entry_id"] != target_entry
    assert restored["next_step"] == "一面"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE source='manual'"
        ).fetchone() == (1,)


def test_prepare_rejects_invalid_application_date_as_safe_conflict(db_path):
    target = _record(
        db_path,
        history_step="完成投递",
        projected_stage="applied",
        current_step="完成投递",
        summary="投递复盘",
    )
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET applied_date='2026-99-99' WHERE user_id='u1'"
        )

    with pytest.raises(operations.ReviewOperationConflict):
        operations.prepare_review_undo_operation(db_path, "u1", journal_id=target)


def test_non_real_question_occurrence_is_not_promised_or_archived(db_path):
    target = _record(db_path, question="后来改成生成题的题目？")
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE questions SET source='generated' "
            "WHERE user_id='u1' AND text='后来改成生成题的题目？'"
        )

    proposal = operations.prepare_review_undo_operation(db_path, "u1")
    assert proposal["target"]["journal_id"] == target
    assert proposal["effect"]["questions_archived"] == []
    completed = operations.approve_review_operation(db_path, "u1", proposal["operation_id"])
    assert completed["result"]["removed"]["questions_archived"] == 0
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT source, status FROM questions "
            "WHERE user_id='u1' AND text='后来改成生成题的题目？'"
        ).fetchone() == ("generated", "active")


def test_cross_tenant_question_occurrence_is_rejected_before_preview(db_path):
    target = _record(db_path, summary="不能绑定其它租户题目的复盘")
    _record(
        db_path,
        user_id="u2",
        company="其它租户公司",
        position="其它岗位",
        question="其它租户的题？",
    )
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "SELECT application_id FROM timeline_entries "
            "WHERE user_id='u1' AND journal_id=?",
            (target,),
        ).fetchone()[0]
        foreign_question_id = conn.execute(
            "SELECT id FROM questions WHERE user_id='u2' AND text='其它租户的题？'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO review_question_occurrences "
            "(user_id, journal_id, question_id, application_id, company, source_step, asked_date) "
            "VALUES ('u1', ?, ?, ?, '腾讯', '一面', '2026-07-01')",
            (target, foreign_question_id, application_id),
        )

    with pytest.raises(operations.ReviewOperationConflict, match="同租户 question"):
        operations.prepare_review_undo_operation(db_path, "u1")


def test_write_locator_uses_exact_names_and_sorts_ambiguity_options(db_path):
    _record(db_path, company="腾讯云", position="平台", summary="腾讯云复盘")
    assert operations.prepare_review_undo_operation(
        db_path,
        "u1",
        company="腾讯",
    ) == {"status": "not_found"}

    review_a = _record(db_path, company="腾讯", position="A 岗", summary="腾讯 A 岗")
    review_b = _record(db_path, company="腾讯", position="B 岗", summary="腾讯 B 岗")
    ambiguous = operations.prepare_review_undo_operation(db_path, "u1", company="腾讯")
    assert ambiguous == {
        "status": "ambiguous",
        "options": ["腾讯·A 岗", "腾讯·B 岗"],
    }

    exact = operations.prepare_review_undo_operation(
        db_path,
        "u1",
        company="腾讯",
        position="A 岗",
    )
    assert exact["target"]["journal_id"] == review_a
    assert exact["target"]["journal_id"] != review_b


def test_write_locator_rejects_more_than_bounded_identity_options(db_path):
    timestamp = "2026-07-01T00:00:00+00:00"
    with transaction(db_path) as conn:
        for index in range(operations.MAX_REVIEW_SELECTOR_OPTIONS + 1):
            application_id = conn.execute(
                "INSERT INTO applications "
                "(user_id, company, position, stage, current_step, created_time, updated_time) "
                "VALUES ('u1', '候选公司', ?, 'applied', '完成投递', ?, ?)",
                (f"岗位 {index:03d}", timestamp, timestamp),
            ).lastrowid
            journal_id = conn.execute(
                "INSERT INTO journal "
                "(user_id, kind, content, created_time, processed_time, extraction_json, "
                "derivation_json, state, revision) VALUES "
                "('u1', 'review', ?, ?, ?, '{}', '{}', 'applied', 1)",
                (f"候选 {index}", timestamp, timestamp),
            ).lastrowid
            conn.execute(
                "INSERT INTO timeline_entries "
                "(user_id, application_id, step, occurred_date, summary, from_stage, "
                "to_stage, to_step, source, journal_id, created_time) VALUES "
                "('u1', ?, '完成投递', '2026-07-01', '已投递', 'backlog', "
                "'applied', '完成投递', 'review', ?, ?)",
                (application_id, journal_id, timestamp),
            )

    with pytest.raises(operations.ReviewOperationConflict, match="过多"):
        operations.prepare_review_undo_operation(db_path, "u1", company="候选公司")


def test_concurrent_approve_replays_one_atomic_receipt(db_path):
    target = _record(db_path, question="并发只能归档一次？", with_status_log=True)
    proposal = operations.prepare_review_undo_operation(db_path, "u1")

    def approve():
        return operations.approve_review_operation(db_path, "u1", proposal["operation_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: approve(), range(2)))

    assert results[0] == results[1]
    assert results[0]["state"] == "completed"
    assert _journal_state(db_path, target)[0] == "voided"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_entries WHERE journal_id=?",
            (target,),
        ).fetchone() == (0,)


def test_review_operation_http_contract_and_canonical_recovery(client):
    test_client, db_path = client
    target = _record(db_path, user_id="me", summary="HTTP 撤销目标")
    proposal = operations.prepare_review_undo_operation(db_path, "me")
    base = f"/api/reviews/undo-operations/{proposal['operation_id']}"

    pending = test_client.get("/api/reviews/undo-operations/pending")
    canonical = test_client.get(base)
    assert pending.status_code == canonical.status_code == 200
    assert pending.json()["operations"] == [proposal]
    assert canonical.json() == proposal
    assert test_client.post(f"{base}/approve", json={"journal_id": target}).status_code == 422

    completed = test_client.post(f"{base}/approve", json={})
    replay = test_client.post(f"{base}/approve", json={})
    recovered = test_client.get(base)
    assert completed.status_code == replay.status_code == recovered.status_code == 200
    assert completed.json() == replay.json() == recovered.json()
    assert completed.json()["state"] == "completed"
    assert test_client.post(f"{base}/reject", json={}).status_code == 409

    assert test_client.get(base, headers={"Remote-User": "other-user"}).status_code == 404
    assert test_client.post(
        f"{base}/approve",
        json={},
        headers={"Remote-User": "other-user"},
    ).status_code == 404
    unknown = "00000000-0000-4000-8000-000000000000"
    assert test_client.get(f"/api/reviews/undo-operations/{unknown}").status_code == 404

    retained = _record(
        db_path,
        user_id="me",
        company="B",
        position="P",
        summary="HTTP 拒绝目标",
    )
    reject_proposal = operations.prepare_review_undo_operation(db_path, "me")
    reject_base = f"/api/reviews/undo-operations/{reject_proposal['operation_id']}"
    rejected = test_client.post(f"{reject_base}/reject", json={})
    rejected_replay = test_client.post(f"{reject_base}/reject", json={})
    assert rejected.status_code == rejected_replay.status_code == 200
    assert rejected.json() == rejected_replay.json()
    assert rejected.json()["state"] == "rejected"
    assert _journal_state(db_path, retained)[0] == "applied"
    assert test_client.post(f"{reject_base}/approve", json={}).status_code == 409

    _record(db_path, user_id="me", company="C", position="P", summary="HTTP 漂移目标")
    drifted_proposal = operations.prepare_review_undo_operation(db_path, "me")
    assert _edit_timeline_entry(db_path, "me", "C", "P", step="二面")["state"] == "completed"
    drifted_base = f"/api/reviews/undo-operations/{drifted_proposal['operation_id']}"
    assert test_client.post(f"{drifted_base}/approve", json={}).status_code == 409
    assert test_client.get(drifted_base).json()["state"] == "stale"
