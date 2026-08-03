"""Stable ``record_review`` operation identity, recovery and two-phase safety."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.preferences.operations import execute_preference_update_operation
from careerdesk.features.reviews.operations import approve_review_operation
from careerdesk.features.reviews.operations import record as record_operation_runtime
from careerdesk.features.reviews.operations.edit import (
    execute_review_timeline_entry_edit_operation,
)
from careerdesk.features.reviews.operations.record import (
    ReviewRecordOperationConflict,
    ReviewRecordOperationNotFound,
    approve_review_record_operation,
    decide_review_record_operations_for_turn,
    execute_review_record_operation,
    get_review_record_operation,
    list_pending_review_record_confirmations,
    list_pending_review_record_clarifications,
    list_review_record_operations_for_turn,
    prepare_review_record_undo_operation,
    reject_review_record_operation,
    recover_interrupted_review_record_operations_in_transaction,
)
from careerdesk.features.reviews.operations.record_models import (
    MAX_PENDING_REVIEW_RECORD_CLARIFICATIONS,
    MAX_REVIEW_RECORD_OPERATIONS_PER_TURN,
    MAX_REVIEW_RECORD_SOURCE_CHARS,
    ReviewRecordDecision,
    ReviewRecordNewTargetPlan,
    ReviewRecordPreview,
)

TODAY = "2026-07-14"
_DEFAULT_CURRENT_STEP = object()


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "record-operations.db")
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


def extraction(
    *,
    company: str | None = "A",
    position: str | None = "P",
    step: str | None = "一面",
    occurred_date: str | None = TODAY,
    stage: str = "interviewing",
    current_step: str | None | object = _DEFAULT_CURRENT_STEP,
    next_action: dict | None = None,
) -> dict:
    return {
        "company": company,
        "position": position,
        "channel": None,
        "history": {
            "step": step,
            "date": occurred_date,
            "outcome": None,
            "summary": "完成一面",
        },
        "projected_state": {
            "stage": stage,
            "current_step": step if current_step is _DEFAULT_CURRENT_STEP else current_step,
        },
        "next_action": next_action,
        "questions": [{
            "text": "如何保证任务幂等？",
            "stuck": True,
            "knowledge_points": ["任务幂等"],
        }],
        "mood": "有点紧张",
        "time_of_day": "afternoon",
        "factors": ["睡眠不足"],
    }


def run(coroutine):
    return asyncio.run(coroutine)


def execute(
    db_path: str,
    payload: dict,
    *,
    user_id: str = "u1",
    operation_id=None,
    client_turn_id=None,
    text: str = "今天完成了 A 的一面",
    review_reference=None,
    extractor=None,
    approve: bool = True,
) -> dict:
    operation_id = operation_id or uuid4()
    client_turn_id = client_turn_id or uuid4()

    async def default_extractor(_combined: str, _today: str):
        return payload

    result = run(execute_review_record_operation(
        db_path,
        user_id,
        operation_id=operation_id,
        client_turn_id=client_turn_id,
        text=text,
        review_reference=review_reference,
        effective_date=TODAY,
        extractor=extractor or default_extractor,
    ))
    if approve and result["state"] == "pending_confirmation":
        return approve_review_record_operation(db_path, user_id, operation_id)
    return result


def scalar(db_path: str, sql: str, *params):
    with read_connection(db_path) as conn:
        return conn.execute(sql, params).fetchone()[0]


def test_target_plan_rejects_impossible_calendar_dates():
    with pytest.raises(ValueError, match="ISO YYYY-MM-DD"):
        ReviewRecordNewTargetPlan(
            kind="new",
            company="A",
            position="P",
            current_stage="backlog",
            current_step=None,
            projected_stage="backlog",
            projected_step=None,
            current_next_action=None,
            projected_next_action={
                "stage": "backlog",
                "step": "跟进",
                "date": "2026-02-31",
                "time": None,
                "note": None,
            },
            current_applied_date=None,
            projected_applied_date=None,
            current_channel=None,
            projected_channel=None,
        )


def test_explicit_position_never_falls_back_to_unique_company_application(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', '真实岗位', ?, ?)",
            (now_iso(), now_iso()),
        )
    operation_id = uuid4()
    turn_id = uuid4()
    staged = execute(
        db_path,
        extraction(position="口语里的那个岗"),
        operation_id=operation_id,
        client_turn_id=turn_id,
        approve=False,
    )
    assert staged["preview"]["target_plan"] == {
        "kind": "new",
        "company": "A",
        "position": "口语里的那个岗",
        "current_stage": "backlog",
        "current_step": None,
        "projected_stage": "interviewing",
        "projected_step": "一面",
        "current_next_action": None,
        "projected_next_action": None,
        "current_applied_date": None,
        "projected_applied_date": None,
        "current_channel": None,
        "projected_channel": None,
    }
    result = approve_review_record_operation(db_path, "u1", operation_id)

    assert result["state"] == "completed" and result["outcome"] == "applied"
    assert result["target_current_state"] == "applied"
    assert result["target_current_revision"] == result["result"]["target_revision"]
    assert result["undo_available"] is True and result["undo_block_reason"] is None
    assert result["result"]["application"]["position"] == "口语里的那个岗"
    assert result["result"]["extraction"]["position"] == "口语里的那个岗"
    assert result["result"]["derivation"]["application_created"] is True
    assert result["review_reference"] == str(operation_id)
    with read_connection(db_path) as conn:
        applications = conn.execute(
            "SELECT position, stage FROM applications WHERE user_id = 'u1' ORDER BY id",
        ).fetchall()
        event_application_id = conn.execute(
            "SELECT application_id FROM timeline_entries WHERE user_id = 'u1'",
        ).fetchone()[0]
    assert [tuple(row) for row in applications] == [
        ("真实岗位", "backlog"),
        ("口语里的那个岗", "interviewing"),
    ]
    assert event_application_id == result["result"]["application"]["id"]
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1
    assert scalar(db_path, "SELECT COUNT(*) FROM status_log") == 1
    assert scalar(db_path, "SELECT COUNT(*) FROM journal WHERE kind='review'") == 1
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='review' AND operation_id IS NULL",
    ) == 1
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id=?",
        str(operation_id),
    ) == 1
    assert get_review_record_operation(db_path, "u1", operation_id) == result
    assert list_review_record_operations_for_turn(db_path, "u1", turn_id) == [result]

    undo = prepare_review_record_undo_operation(db_path, "u1", operation_id)
    assert undo["state"] == "pending"
    assert undo["target"]["journal_id"] == result["target_journal_id"]
    assert undo["operation_id"] != result["operation_id"]


def test_existing_application_result_allows_omitted_position_but_rejects_mismatch(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', '真实岗位', ?, ?)",
            (now_iso(), now_iso()),
        )

    result = execute(db_path, extraction(position=None))

    assert result["result"]["derivation"]["application_created"] is False
    assert result["result"]["extraction"]["position"] is None
    assert result["result"]["application"]["position"] == "真实岗位"

    with transaction(db_path) as conn:
        raw = conn.execute(
            "SELECT derivation_json FROM journal WHERE operation_id=?",
            (result["operation_id"],),
        ).fetchone()[0]
        receipt = json.loads(raw)
        receipt["operation"]["result"]["extraction"]["position"] = "伪造岗位"
        conn.execute(
            "UPDATE journal SET derivation_json=? WHERE operation_id=?",
            (json.dumps(receipt, ensure_ascii=False), result["operation_id"]),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="回执已损坏"):
        get_review_record_operation(db_path, "u1", result["operation_id"])


def test_extraction_stages_cross_refresh_preview_before_any_business_projection(db_path):
    operation_id = uuid4()
    staged = execute(
        db_path,
        extraction(step=None, occurred_date=None),
        operation_id=operation_id,
        approve=False,
    )

    assert staged["state"] == "pending_confirmation"
    assert staged["terminal"] is False
    assert staged["preview"]["extraction"]["history"]["step"] is None
    assert staged["preview"]["extraction"]["history"]["date"] is None
    assert staged["preview"]["missing"] == []
    assert staged["preview"]["target_plan"] == {
        "kind": "new",
        "company": "A",
        "position": "P",
        "current_stage": "backlog",
        "current_step": None,
        "projected_stage": "interviewing",
        "projected_step": None,
        "current_next_action": None,
        "projected_next_action": None,
        "current_applied_date": None,
        "projected_applied_date": None,
        "current_channel": None,
        "projected_channel": None,
    }
    assert list_pending_review_record_confirmations(db_path, "u1") == [staged]
    for table in ("companies", "applications", "timeline_entries", "questions", "status_log"):
        assert scalar(db_path, f"SELECT COUNT(*) FROM {table}") == 0

    approved = approve_review_record_operation(db_path, "u1", operation_id)
    assert approved["state"] == "completed" and approved["outcome"] == "applied"
    assert approved["result"]["extraction"]["history"]["step"] is None
    assert approved["result"]["extraction"]["history"]["date"] is None
    assert list_pending_review_record_confirmations(db_path, "u1") == []
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1
    assert scalar(db_path, "SELECT COUNT(*) FROM status_log") == 0
    assert reject_review_record_operation(db_path, "u1", operation_id) == approved


def test_channel_is_frozen_in_preview_and_written_to_application(db_path):
    payload = extraction(step=None, occurred_date="2026-07-17")
    payload["channel"] = "Boss直聘"
    payload["projected_state"] = {"stage": "applied", "current_step": "已提交"}
    operation_id = uuid4()

    staged = execute(db_path, payload, operation_id=operation_id, approve=False)
    plan = staged["preview"]["target_plan"]
    assert (plan["current_channel"], plan["projected_channel"]) == (
        None,
        "Boss直聘",
    )

    approved = approve_review_record_operation(db_path, "u1", operation_id)
    assert approved["result"]["extraction"]["channel"] == "Boss直聘"
    assert scalar(db_path, "SELECT channel FROM applications") == "Boss直聘"


def test_matching_completed_step_clears_frozen_next_action_after_confirmation(db_path):
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications ("
            "user_id, company, position, stage, current_step, next_stage, next_step, "
            "next_date, created_time, updated_time"
            ") VALUES ('u1', 'A', 'P', 'interviewing', '简历筛选', "
            "'interviewing', '一面', '2026-07-20', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
    operation_id = uuid4()

    staged = execute(
        db_path,
        extraction(step="一面", current_step="一面"),
        operation_id=operation_id,
        approve=False,
    )

    assert staged["preview"]["extraction"]["clear_next_action"] is True
    plan = staged["preview"]["target_plan"]
    assert plan["application_id"] == application_id
    assert plan["current_next_action"]["step"] == "一面"
    assert plan["projected_next_action"] is None

    approved = approve_review_record_operation(db_path, "u1", operation_id)
    assert approved["result"]["extraction"]["clear_next_action"] is True
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT stage, current_step, next_stage, next_step, next_date "
            "FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
        entry = conn.execute(
            "SELECT step, summary FROM timeline_entries WHERE application_id = ?",
            (application_id,),
        ).fetchone()
    assert tuple(row) == ("interviewing", "一面", None, None, None)
    assert tuple(entry) == ("一面", "完成一面")


def test_user_can_edit_matching_completion_back_to_preserve_frozen_plan(db_path):
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications ("
            "user_id, company, position, stage, current_step, next_stage, next_step, "
            "next_date, created_time, updated_time"
            ") VALUES ('u1', 'A', 'P', 'interviewing', '简历筛选', "
            "'interviewing', '一面', '2026-07-20', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
    turn_id = uuid4()
    staged = execute(
        db_path,
        extraction(step="一面", current_step="一面"),
        client_turn_id=turn_id,
        approve=False,
    )
    assert staged["preview"]["extraction"]["clear_next_action"] is True
    edited = {
        **staged["preview"]["extraction"],
        "clear_next_action": False,
    }

    [approved] = decide_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
        [{
            "operation_id": staged["operation_id"],
            "action": "approve",
            "edited_extraction": edited,
        }],
    )

    assert approved["result"]["extraction"]["clear_next_action"] is False
    assert scalar(
        db_path,
        "SELECT next_step FROM applications WHERE id=?",
        application_id,
    ) == "一面"


def test_terminal_review_explicitly_clears_plan_and_cannot_be_edited_to_preserve(db_path):
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications ("
            "user_id, company, position, stage, current_step, next_stage, next_step, "
            "next_date, created_time, updated_time"
            ") VALUES ('u1', 'A', 'P', 'interviewing', '一面', "
            "'interviewing', '二面', '2026-07-20', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
    turn_id = uuid4()
    staged = execute(
        db_path,
        extraction(step="收到结果", stage="rejected", current_step="流程结束"),
        client_turn_id=turn_id,
        approve=False,
    )

    assert staged["preview"]["extraction"]["clear_next_action"] is True
    edited = {
        **staged["preview"]["extraction"],
        "clear_next_action": False,
    }
    with pytest.raises(ReviewRecordOperationConflict, match="没有可发布的有效进展"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [{
                "operation_id": staged["operation_id"],
                "action": "approve",
                "edited_extraction": edited,
            }],
        )

    approved = approve_review_record_operation(db_path, "u1", staged["operation_id"])
    assert approved["state"] == "completed"
    assert scalar(db_path, "SELECT next_step FROM applications WHERE id=?", application_id) is None


def test_unrelated_completed_step_preserves_existing_next_action(db_path):
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications ("
            "user_id, company, position, stage, current_step, next_stage, next_step, "
            "next_date, created_time, updated_time"
            ") VALUES ('u1', 'A', 'P', 'applied', '简历筛选', "
            "'interviewing', '一面', '2026-07-20', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
    payload = extraction(step="联系 HR", stage="applied", current_step="联系 HR")

    staged = execute(db_path, payload, approve=False)
    assert staged["preview"]["extraction"]["clear_next_action"] is False
    assert staged["preview"]["target_plan"]["projected_next_action"]["step"] == "一面"
    approved = approve_review_record_operation(db_path, "u1", staged["operation_id"])
    assert approved["state"] == "completed"
    assert scalar(
        db_path,
        "SELECT next_step FROM applications WHERE id=?",
        application_id,
    ) == "一面"


def test_missing_position_with_no_existing_application_retains_draft(db_path):
    operation_id = uuid4()
    staged = execute(
        db_path,
        extraction(position=None),
        operation_id=operation_id,
        approve=False,
    )

    assert staged["preview"]["target_plan"] is None
    assert [item["field"] for item in staged["preview"]["missing"]] == ["position"]
    retained = approve_review_record_operation(db_path, "u1", operation_id)
    assert retained["state"] == "completed"
    assert retained["outcome"] == "needs_clarification"
    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_existing_target_delete_and_recreate_supersedes_frozen_plan(db_path):
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P', ?, ?)",
            (now_iso(), now_iso()),
        )
        original_id = cursor.lastrowid
    operation_id = uuid4()
    staged = execute(db_path, extraction(), operation_id=operation_id, approve=False)
    assert staged["preview"]["target_plan"]["application_id"] == original_id

    with transaction(db_path) as conn:
        conn.execute("DELETE FROM applications WHERE user_id='u1' AND id=?", (original_id,))
        replacement = conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
    assert replacement != original_id

    superseded = approve_review_record_operation(db_path, "u1", operation_id)
    assert superseded["state"] == "superseded"
    assert superseded["error"]["code"] == "target_changed"
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_existing_target_stage_drift_supersedes_frozen_effect(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P', ?, ?)",
            (now_iso(), now_iso()),
        )
    operation_id = uuid4()
    staged = execute(db_path, extraction(), operation_id=operation_id, approve=False)
    assert staged["preview"]["target_plan"]["current_stage"] == "backlog"
    assert staged["preview"]["target_plan"]["projected_stage"] == "interviewing"

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET stage='applied' WHERE user_id='u1' AND company='A'",
        )
    superseded = approve_review_record_operation(db_path, "u1", operation_id)

    assert superseded["state"] == "superseded"
    assert superseded["error"]["code"] == "target_changed"
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_existing_target_next_action_drift_supersedes_frozen_effect(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, next_stage, next_step, "
            "next_date, next_note, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P', 'interviewing', '二面', '2026-08-01', '原安排', ?, ?)",
            (now_iso(), now_iso()),
        )
    payload = extraction(next_action={
        "stage": "interviewing",
        "step": "三面",
        "date": "2026-08-10",
        "time": None,
        "note": None,
    })
    operation_id = uuid4()
    staged = execute(db_path, payload, operation_id=operation_id, approve=False)
    plan = staged["preview"]["target_plan"]
    assert plan["current_next_action"] == {
        "stage": "interviewing", "step": "二面", "date": "2026-08-01",
        "time": None, "note": "原安排",
    }
    assert plan["projected_next_action"] == payload["next_action"]

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET next_date='2026-08-05', "
            "next_note='并发安排' WHERE user_id='u1' AND company='A'",
        )
    superseded = approve_review_record_operation(db_path, "u1", operation_id)

    assert superseded["state"] == "superseded"
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    assert scalar(
        db_path,
        "SELECT next_date FROM applications WHERE user_id='u1' AND company='A'",
    ) == "2026-08-05"


def test_existing_target_revision_aba_supersedes_frozen_effect(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P', ?, ?)",
            (now_iso(), now_iso()),
        )
    operation_id = uuid4()
    staged = execute(db_path, extraction(), operation_id=operation_id, approve=False)
    assert staged["preview"]["target_plan"]["revision"] == 0

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET revision = revision + 1 "
            "WHERE user_id='u1' AND company='A'",
        )
    superseded = approve_review_record_operation(db_path, "u1", operation_id)

    assert superseded["state"] == "superseded"
    assert superseded["error"]["code"] == "target_changed"
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


@pytest.mark.parametrize(
    ("column", "plan_key", "replacement"),
    [
        ("applied_date", "current_applied_date", "2026-07-01"),
        ("channel", "current_channel", "内推"),
    ],
)
def test_existing_target_application_lifecycle_drift_supersedes_frozen_effect(
    db_path,
    column,
    plan_key,
    replacement,
):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P', ?, ?)",
            (now_iso(), now_iso()),
        )
    operation_id = uuid4()
    staged = execute(db_path, extraction(), operation_id=operation_id, approve=False)
    assert staged["preview"]["target_plan"][plan_key] is None

    with transaction(db_path) as conn:
        conn.execute(
            f"UPDATE applications SET {column}=? WHERE user_id='u1' AND company='A'",
            (replacement,),
        )
    superseded = approve_review_record_operation(db_path, "u1", operation_id)

    assert superseded["state"] == "superseded"
    assert superseded["error"]["code"] == "target_changed"
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_applied_history_preview_and_result_include_exact_application_lifecycle_effects(db_path):
    payload = extraction(
        step="提交申请",
        occurred_date="2026-07-10",
        stage="applied",
        current_step="已提交",
    )
    operation_id = uuid4()
    staged = execute(db_path, payload, operation_id=operation_id, approve=False)
    plan = staged["preview"]["target_plan"]

    assert (plan["current_applied_date"], plan["projected_applied_date"]) == (
        None,
        "2026-07-10",
    )
    assert (plan["current_step"], plan["projected_step"]) == (None, "已提交")

    applied = approve_review_record_operation(db_path, "u1", operation_id)
    assert applied["state"] == "completed"
    assert scalar(
        db_path,
        "SELECT applied_date FROM applications WHERE user_id='u1' AND company='A'",
    ) == "2026-07-10"
    assert scalar(
        db_path,
        "SELECT current_step FROM applications WHERE user_id='u1' AND company='A'",
    ) == "已提交"


def test_explicit_same_applied_stage_does_not_infer_missing_applied_date(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, stage, current_step, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P', 'applied', '已提交', ?, ?)",
            (now_iso(), now_iso()),
        )
    payload = extraction(
        step="复盘申请材料",
        occurred_date="2026-07-10",
        stage="applied",
        current_step="材料复盘",
    )
    operation_id = uuid4()

    staged = execute(db_path, payload, operation_id=operation_id, approve=False)
    plan = staged["preview"]["target_plan"]

    assert plan["current_stage"] == plan["projected_stage"] == "applied"
    assert (plan["current_step"], plan["projected_step"]) == ("已提交", "材料复盘")
    assert plan["current_applied_date"] is None
    assert plan["projected_applied_date"] is None

    applied = approve_review_record_operation(db_path, "u1", operation_id)
    assert applied["state"] == "completed"
    assert applied["result"]["derivation"]["application_after"]["applied_date"] is None
    assert scalar(
        db_path,
        "SELECT applied_date FROM applications WHERE user_id='u1' AND company='A'",
    ) is None


def test_stage_only_review_does_not_promote_history_label_to_current_step(db_path):
    payload = extraction(
        step="参加宣讲会",
        occurred_date="2026-07-10",
        stage="applied",
        current_step=None,
    )
    staged = execute(db_path, payload, approve=False)

    assert staged["preview"]["target_plan"]["projected_stage"] == "applied"
    assert staged["preview"]["target_plan"]["projected_step"] is None
    approved = approve_review_record_operation(db_path, "u1", staged["operation_id"])

    assert approved["state"] == "completed"
    assert scalar(
        db_path,
        "SELECT current_step FROM applications WHERE user_id='u1' AND company='A'",
    ) is None


def test_unapproved_exact_text_cannot_create_a_second_target_or_card(db_path):
    text = "今天完成了 A 的一面"
    first = execute(db_path, extraction(), text=text, approve=False)
    before_journal = scalar(db_path, "SELECT COUNT(*) FROM journal")

    with pytest.raises(ReviewRecordOperationConflict, match="相同原文已有一条复盘草稿"):
        execute(db_path, extraction(), text=text, approve=False)

    assert scalar(db_path, "SELECT COUNT(*) FROM journal") == before_journal
    assert list_pending_review_record_confirmations(db_path, "u1") == [first]
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_rejecting_pending_preview_voids_source_without_business_projection(db_path):
    operation_id = uuid4()
    staged = execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        approve=False,
    )

    rejected = reject_review_record_operation(db_path, "u1", operation_id)

    assert staged["state"] == "pending_confirmation"
    assert rejected["state"] == "rejected" and rejected["terminal"] is True
    assert reject_review_record_operation(db_path, "u1", operation_id) == rejected
    assert approve_review_record_operation(db_path, "u1", operation_id) == rejected
    assert list_pending_review_record_confirmations(db_path, "u1") == []
    assert scalar(db_path, "SELECT state FROM journal WHERE kind='review'") == "voided"
    for table in ("companies", "applications", "timeline_entries", "questions", "status_log"):
        assert scalar(db_path, f"SELECT COUNT(*) FROM {table}") == 0


def test_pending_confirmation_actions_and_list_are_tenant_scoped(db_path):
    operation_id = uuid4()
    staged = execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        approve=False,
    )

    assert list_pending_review_record_confirmations(db_path, "u2") == []
    for action in (approve_review_record_operation, reject_review_record_operation):
        with pytest.raises(ReviewRecordOperationNotFound):
            action(db_path, "u2", operation_id)
    assert list_pending_review_record_confirmations(db_path, "u1") == [staged]
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_approval_rechecks_new_position_ambiguity_and_retains_draft(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P1', ?, ?)",
            (now_iso(), now_iso()),
        )
    operation_id = uuid4()
    staged = execute(
        db_path,
        extraction(position=None),
        operation_id=operation_id,
        approve=False,
    )
    assert staged["preview"]["missing"] == []

    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P2', ?, ?)",
            (now_iso(), now_iso()),
        )
    retained = approve_review_record_operation(db_path, "u1", operation_id)

    assert retained["state"] == "superseded"
    assert retained["error"]["code"] == "target_changed"
    assert retained["target_current_state"] == "pending"
    assert list_pending_review_record_confirmations(db_path, "u1") == []
    for table in ("timeline_entries", "questions", "knowledge_points", "status_log"):
        assert scalar(db_path, f"SELECT COUNT(*) FROM {table}") == 0


def test_target_revision_drift_supersedes_preview_without_projection(db_path):
    operation_id = uuid4()
    staged = execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        approve=False,
    )
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET revision = revision + 1 WHERE id = ? AND kind = 'review'",
            (staged["target_journal_id"],),
        )

    superseded = approve_review_record_operation(db_path, "u1", operation_id)

    assert superseded["state"] == "superseded"
    assert superseded["error"]["code"] == "source_changed"
    assert reject_review_record_operation(db_path, "u1", operation_id) == superseded
    assert list_pending_review_record_confirmations(db_path, "u1") == []
    for table in ("companies", "applications", "timeline_entries", "questions", "status_log"):
        assert scalar(db_path, f"SELECT COUNT(*) FROM {table}") == 0


def test_pending_preview_supplement_supersedes_old_card_then_approves(db_path):
    first_id = uuid4()
    first = execute(
        db_path,
        extraction(company=None, position=None),
        operation_id=first_id,
        text="面完了但还没说公司",
        approve=False,
    )
    assert first["state"] == "pending_confirmation"
    assert [item["field"] for item in first["preview"]["missing"]] == ["company"]

    supplemented = execute(
        db_path,
        extraction(company="A", position="P"),
        operation_id=uuid4(),
        text="补充：A 公司 P 岗",
        review_reference=first_id,
        approve=False,
    )

    old = get_review_record_operation(db_path, "u1", first_id)
    assert old is not None and old["state"] == "superseded"
    assert supplemented["state"] == "pending_confirmation"
    assert supplemented["target_journal_id"] == first["target_journal_id"]
    assert list_pending_review_record_confirmations(db_path, "u1") == [supplemented]
    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0

    approved = approve_review_record_operation(
        db_path,
        "u1",
        supplemented["operation_id"],
    )
    assert approved["state"] == "completed" and approved["outcome"] == "applied"
    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 1
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1


def test_stale_root_reference_supersedes_existing_pending_supplement(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None),
        text="最初只记得面试了",
    )
    first = execute(
        db_path,
        extraction(company="A", position="P"),
        text="第一次补充 A 公司",
        review_reference=initial["review_reference"],
        approve=False,
    )
    latest = execute(
        db_path,
        extraction(company="A", position="P"),
        text="再补充确认是 P 岗",
        review_reference=initial["review_reference"],
        approve=False,
    )

    historical = get_review_record_operation(db_path, "u1", first["operation_id"])
    assert historical is not None and historical["state"] == "superseded"
    assert list_pending_review_record_confirmations(db_path, "u1") == [latest]
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_rejecting_supplement_preview_discards_only_that_supplement(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None),
        text="面完了但还没说公司",
    )
    supplement = execute(
        db_path,
        extraction(company="B", position="P"),
        text="误补充成 B 公司",
        review_reference=initial["review_reference"],
        approve=False,
    )

    rejected = reject_review_record_operation(
        db_path,
        "u1",
        supplement["operation_id"],
    )

    assert rejected["state"] == "rejected"
    assert rejected["target_current_state"] == "awaiting_user"
    assert get_review_record_operation(
        db_path,
        "u1",
        supplement["operation_id"],
    ) == rejected
    [retained_initial] = list_pending_review_record_clarifications(db_path, "u1")
    assert retained_initial["operation_id"] == initial["operation_id"]
    assert retained_initial["outcome"] == "needs_clarification"
    assert scalar(
        db_path,
        "SELECT state FROM journal WHERE id=?",
        supplement["source_journal_id"],
    ) == "voided"
    for table in ("applications", "timeline_entries", "questions", "knowledge_points", "status_log"):
        assert scalar(db_path, f"SELECT COUNT(*) FROM {table}") == 0


def test_startup_recovery_preserves_pending_confirmation(db_path):
    staged = execute(db_path, extraction(), approve=False)

    with transaction(db_path) as conn:
        recovered = recover_interrupted_review_record_operations_in_transaction(conn)

    assert recovered == {"operations": 0, "reviews": 0}
    assert get_review_record_operation(db_path, "u1", staged["operation_id"]) == staged
    assert list_pending_review_record_confirmations(db_path, "u1") == [staged]


def test_pending_confirmation_list_fails_closed_for_damaged_preview_action(db_path):
    staged = execute(db_path, extraction(), approve=False)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET derivation_json=json_set(derivation_json, "
            "'$.operation.action', 'damaged') WHERE operation_id=?",
            (staged["operation_id"],),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="待确认复盘预览身份已损坏"):
        list_pending_review_record_confirmations(db_path, "u1")


@pytest.mark.parametrize(
    ("json_path", "damaged_value"),
    [
        ("$.operation.preview.target_plan.projected_stage", "offer"),
        ("$.operation.preview.target_plan.projected_step", "终面"),
        ("$.operation.preview.target_plan.projected_applied_date", "2026-07-01"),
        ("$.operation.preview.target_plan.projected_channel", "伪造渠道"),
    ],
)
def test_pending_confirmation_list_rejects_semantically_tampered_effect(
    db_path,
    json_path,
    damaged_value,
):
    staged = execute(db_path, extraction(), approve=False)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET derivation_json=json_set(derivation_json, ?, ?) "
            "WHERE operation_id=?",
            (json_path, damaged_value, staged["operation_id"]),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="待确认复盘预览身份已损坏"):
        list_pending_review_record_confirmations(db_path, "u1")


def test_pending_confirmation_list_fails_closed_for_damaged_proposal(db_path):
    staged = execute(db_path, extraction(), approve=False)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET extraction_json='not-json' WHERE operation_id=?",
            (staged["operation_id"],),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="复盘操作命令 不是 JSON object"):
        list_pending_review_record_confirmations(db_path, "u1")


def test_pending_confirmation_list_fails_closed_for_duplicate_target(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None),
        text="初始身份缺失",
    )
    staged = execute(
        db_path,
        extraction(),
        text="第一次补充",
        review_reference=initial["review_reference"],
        approve=False,
    )
    duplicate_operation_id = str(uuid4())
    duplicate_turn_id = str(uuid4())
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT extraction_json, derivation_json, parent_journal_id "
            "FROM journal WHERE operation_id=?",
            (staged["operation_id"],),
        ).fetchone()
        proposal_payload = json.loads(row[0])
        duplicate_source_text = "并发注入的第二条补充"
        timestamp = now_iso()
        source_cursor = conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, processed_time, "
            "derivation_json, state, parent_journal_id) "
            "VALUES ('u1', 'correction', ?, ?, ?, ?, 'applied', ?)",
            (
                duplicate_source_text,
                timestamp,
                timestamp,
                '{"source_type":"review_supplement"}',
                row[2],
            ),
        )
        target_content = conn.execute(
            "SELECT content FROM journal WHERE id=?",
            (row[2],),
        ).fetchone()[0]
        supplements = record_operation_runtime._supplement_rows(conn, "u1", row[2])
        proposal_payload.update({
            "operation_id": duplicate_operation_id,
            "client_turn_id": duplicate_turn_id,
            "attempt_token": str(uuid4()),
            "source_journal_id": source_cursor.lastrowid,
            "source_digest": record_operation_runtime._text_digest(duplicate_source_text),
            "combined_digest": record_operation_runtime._text_digest(
                record_operation_runtime._bounded_combined(
                    target_content,
                    [content for _journal_id, content in supplements],
                ),
            ),
        })
        proposal = record_operation_runtime.ReviewRecordProposal.model_validate(
            proposal_payload,
        )
        preview_payload = json.loads(row[1])
        preview_payload["operation"]["client_turn_id"] = duplicate_turn_id
        preview_payload["operation"]["proposal_digest"] = (
            record_operation_runtime._proposal_digest(proposal)
        )
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, extraction_json, "
            "derivation_json, state, revision, parent_journal_id, operation_id) "
            "VALUES ('u1', 'correction', ?, ?, ?, ?, 'awaiting_user', 1, ?, ?)",
            (
                "[duplicate pending Review owner]",
                now_iso(),
                record_operation_runtime._canonical_json(proposal.model_dump(mode="json")),
                record_operation_runtime._canonical_json(preview_payload),
                row[2],
                duplicate_operation_id,
            ),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="同一复盘存在多条待确认方案"):
        list_pending_review_record_confirmations(db_path, "u1")


def test_same_operation_or_turn_replays_without_second_extractor_call(db_path):
    operation_id = uuid4()
    turn_id = uuid4()
    calls = 0

    async def counted(_combined: str, _today: str):
        nonlocal calls
        calls += 1
        return extraction()

    first = execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        client_turn_id=turn_id,
        extractor=counted,
    )
    same_operation = execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        client_turn_id=turn_id,
        extractor=counted,
    )
    regenerated_operation = execute(
        db_path,
        extraction(),
        operation_id=uuid4(),
        client_turn_id=turn_id,
        extractor=counted,
    )

    assert calls == 1
    assert first == same_operation == regenerated_operation
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1
    with pytest.raises(ReviewRecordOperationConflict):
        execute(
            db_path,
            extraction(),
            operation_id=operation_id,
            client_turn_id=turn_id,
            text="同一身份换成另一条命令",
            extractor=counted,
        )
    assert calls == 1


def test_same_turn_can_own_three_distinct_review_proposals(db_path):
    turn_id = uuid4()
    first = execute(
        db_path,
        extraction(company="百度", position="Agent开发"),
        client_turn_id=turn_id,
        text="昨天面试了百度的 Agent 开发",
        approve=False,
    )
    second = execute(
        db_path,
        extraction(company="重庆理工大学", position="助理教师", step=None),
        client_turn_id=turn_id,
        text="投递了重庆理工大学的助理教师岗位",
        approve=False,
    )
    third = execute(
        db_path,
        extraction(company="交通大学", position="前端工程师", step="二面"),
        client_turn_id=turn_id,
        text="交通大学的前端工程师进入二面",
        approve=False,
    )

    operations = list_review_record_operations_for_turn(db_path, "u1", turn_id)
    assert [item["operation_id"] for item in operations] == [
        first["operation_id"], second["operation_id"], third["operation_id"],
    ]
    assert [item["preview"]["extraction"]["company"] for item in operations] == [
        "百度", "重庆理工大学", "交通大学",
    ]
    assert operations[0]["preview"]["extraction"]["history"]["summary"] == "完成一面"
    assert operations[1]["preview"]["extraction"]["history"]["summary"] == "完成一面"
    assert operations[2]["preview"]["extraction"]["position"] == "前端工程师"


def test_one_batch_decision_approves_selected_and_rejects_excluded_reviews(db_path):
    turn_id = uuid4()
    staged = [
        execute(
            db_path,
            extraction(company=company, position=position),
            client_turn_id=turn_id,
            text=f"完成了 {company} {position} 的一面",
            approve=False,
        )
        for company, position in [("A", "前端"), ("B", "后端"), ("C", "算法")]
    ]
    decisions = [
        {"operation_id": staged[0]["operation_id"], "action": "approve"},
        {"operation_id": staged[1]["operation_id"], "action": "reject"},
        {"operation_id": staged[2]["operation_id"], "action": "approve"},
    ]

    decided = decide_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
        decisions,
    )

    assert [operation["state"] for operation in decided] == [
        "completed", "rejected", "completed",
    ]
    assert scalar(db_path, "SELECT COUNT(*) FROM applications WHERE user_id = 'u1'") == 2
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries WHERE user_id = 'u1'") == 2
    assert decide_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
        decisions,
    ) == decided
    with pytest.raises(ReviewRecordOperationConflict, match="已经完成"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [
                {**decisions[0], "action": "reject"},
                decisions[1],
                decisions[2],
            ],
        )


def test_batch_decision_applies_an_inline_child_edit_in_the_same_confirmation(db_path):
    turn_id = uuid4()
    staged = execute(
        db_path,
        extraction(company="原公司", position="原岗位", step="一面"),
        client_turn_id=turn_id,
        text="完成了原公司原岗位的一面",
        approve=False,
    )
    edited = {
        **staged["preview"]["extraction"],
        "channel": "Boss直聘",
        "history": {
            "step": "二面",
            "date": TODAY,
            "outcome": "passed",
            "summary": "完成二面",
        },
        "projected_state": {"stage": "interviewing", "current_step": "二面"},
    }
    decisions = [{
        "operation_id": staged["operation_id"],
        "action": "approve",
        "edited_extraction": edited,
    }]

    [decided] = decide_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
        decisions,
    )

    assert decided["state"] == "completed"
    assert decided["result"]["extraction"] == edited
    assert decided["result"]["application"]["company"] == "原公司"
    with read_connection(db_path) as conn:
        application = conn.execute(
            "SELECT company, position, channel, current_step FROM applications WHERE user_id = 'u1'",
        ).fetchone()
        entry = conn.execute(
            "SELECT step, outcome, summary FROM timeline_entries WHERE user_id = 'u1'",
        ).fetchone()
    assert tuple(application) == ("原公司", "原岗位", "Boss直聘", "二面")
    assert tuple(entry) == ("二面", "passed", "完成二面")
    assert decide_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
        decisions,
    ) == [decided]
    with pytest.raises(ReviewRecordOperationConflict, match="岗位编辑"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [{
                **decisions[0],
                "edited_extraction": {
                    **edited,
                    "history": {**edited["history"], "step": "三面"},
                },
            }],
        )


def test_batch_decision_cannot_retarget_a_frozen_preview_by_editing_identity(db_path):
    turn_id = uuid4()
    staged = execute(
        db_path,
        extraction(company="原公司", position="原岗位", step="一面"),
        client_turn_id=turn_id,
        text="完成了原公司原岗位的一面",
        approve=False,
    )
    edited = {
        **staged["preview"]["extraction"],
        "company": "另一公司",
        "position": "另一岗位",
    }

    with pytest.raises(ReviewRecordOperationConflict, match="已锁定"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [{
                "operation_id": staged["operation_id"],
                "action": "approve",
                "edited_extraction": edited,
            }],
        )

    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert staged["state"] == "pending_confirmation"


def test_inline_child_edit_is_strict_and_rejects_nested_type_coercion():
    with pytest.raises(ValueError):
        ReviewRecordDecision.model_validate({
            "operation_id": str(uuid4()),
            "action": "approve",
            "edited_extraction": {
                **extraction(),
                "history": {
                    **extraction()["history"],
                    "step": 2,
                },
            },
        })


def test_missing_identity_contract_allows_each_field_at_most_once():
    base = {
        "extraction": extraction(company=None, position=None),
        "target_plan": None,
    }
    ReviewRecordPreview.model_validate({
        **base,
        "missing": [
            {"field": "company", "ask": "是哪家公司？"},
            {"field": "position", "ask": "是什么岗位？"},
        ],
    })

    with pytest.raises(ValueError, match="at most 2 items"):
        ReviewRecordPreview.model_validate({
            **base,
            "missing": [
                {"field": "company", "ask": "是哪家公司？"},
                {"field": "position", "ask": "是什么岗位？"},
                {"field": "company", "ask": "请再次确认公司"},
            ],
        })

    with pytest.raises(ValueError, match="must be unique"):
        ReviewRecordPreview.model_validate({
            **base,
            "missing": [
                {"field": "company", "ask": "是哪家公司？"},
                {"field": "company", "ask": "请再次确认公司"},
            ],
        })


def test_batch_decision_rejects_duplicate_edited_job_identity_atomically(db_path):
    turn_id = uuid4()
    staged = [
        execute(
            db_path,
            extraction(company=company, position=None),
            client_turn_id=turn_id,
            text=f"完成了 {company} 的一面",
            approve=False,
        )
        for company, position in [("A", "前端"), ("B", "后端")]
    ]
    decisions = [{
        "operation_id": operation["operation_id"],
        "action": "approve",
        "edited_extraction": {
            **operation["preview"]["extraction"],
            "company": "同一公司",
            "position": "同一岗位",
        },
    } for operation in staged]

    with pytest.raises(ReviewRecordOperationConflict, match="重复的公司和岗位"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            decisions,
        )

    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    assert [operation["state"] for operation in list_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
    )] == ["pending_confirmation", "pending_confirmation"]


def test_batch_decision_rejects_whitespace_equivalent_new_jobs_atomically(db_path):
    turn_id = uuid4()
    staged = [
        execute(
            db_path,
            extraction(company=company, position=position),
            client_turn_id=turn_id,
            text=f"完成了 {company} {position} 的一面",
            approve=False,
        )
        for company, position in [
            ("示 例 公司", "AI 工程师"),
            ("示例公司", "AI工程师"),
        ]
    ]

    with pytest.raises(ReviewRecordOperationConflict, match="重复的公司和岗位"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [
                {"operation_id": item["operation_id"], "action": "approve"}
                for item in staged
            ],
        )

    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    assert [operation["state"] for operation in list_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
    )] == ["pending_confirmation", "pending_confirmation"]


def test_batch_decision_rejects_two_children_resolved_to_one_existing_job(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', '真实岗位', ?, ?)",
            (now_iso(), now_iso()),
        )
    turn_id = uuid4()
    staged = [
        execute(
            db_path,
            extraction(company="A", position=None),
            client_turn_id=turn_id,
            text=f"完成了 A 的第 {index} 条一面进展",
            approve=False,
        )
        for index in (1, 2)
    ]

    with pytest.raises(ReviewRecordOperationConflict, match="指向同一个岗位"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [{"operation_id": item["operation_id"], "action": "approve"}
             for item in staged],
        )

    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    assert [operation["state"] for operation in list_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
    )] == ["pending_confirmation", "pending_confirmation"]


def test_batch_decision_creates_two_distinct_new_jobs_for_the_same_company(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', '前端', ?, ?)",
            (now_iso(), now_iso()),
        )
    turn_id = uuid4()
    staged = [
        execute(
            db_path,
            extraction(company="A", position=position),
            client_turn_id=turn_id,
            text=f"完成了 A {position} 的一面",
            approve=False,
        )
        for position in ("前端", "后端")
    ]

    decided = decide_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
        [{"operation_id": item["operation_id"], "action": "approve"}
         for item in staged],
    )

    assert [operation["state"] for operation in decided] == ["completed", "completed"]
    with read_connection(db_path) as conn:
        applications = conn.execute(
            "SELECT company, position FROM applications WHERE user_id = 'u1' ORDER BY id",
        ).fetchall()
        event_application_ids = conn.execute(
            "SELECT application_id FROM timeline_entries WHERE user_id = 'u1' ORDER BY id",
        ).fetchall()
    assert [tuple(row) for row in applications] == [("A", "前端"), ("A", "后端")]
    assert len({row[0] for row in event_application_ids}) == 2


def test_batch_decision_rejects_cross_turn_and_cross_tenant_members_without_writes(db_path):
    first_turn = uuid4()
    second_turn = uuid4()
    first = execute(
        db_path,
        extraction(company="A", position="前端"),
        client_turn_id=first_turn,
        approve=False,
    )
    second = execute(
        db_path,
        extraction(company="B", position="后端"),
        client_turn_id=second_turn,
        text="完成了 B 后端的一面",
        approve=False,
    )

    with pytest.raises(ReviewRecordOperationConflict, match="不属于本轮"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            first_turn,
            [
                {"operation_id": first["operation_id"], "action": "approve"},
                {"operation_id": second["operation_id"], "action": "approve"},
            ],
        )
    with pytest.raises(ReviewRecordOperationNotFound):
        decide_review_record_operations_for_turn(
            db_path,
            "u2",
            first_turn,
            [{"operation_id": first["operation_id"], "action": "approve"}],
        )
    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_batch_decision_requires_every_pending_child_and_rolls_back(db_path):
    turn_id = uuid4()
    first = execute(
        db_path,
        extraction(company="A", position="前端"),
        client_turn_id=turn_id,
        text="完成了 A 前端的一面",
        approve=False,
    )
    execute(
        db_path,
        extraction(company="B", position="后端"),
        client_turn_id=turn_id,
        text="完成了 B 后端的一面",
        approve=False,
    )

    with pytest.raises(ReviewRecordOperationConflict, match="遗漏"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [{"operation_id": first["operation_id"], "action": "approve"}],
        )

    assert scalar(db_path, "SELECT COUNT(*) FROM applications WHERE user_id = 'u1'") == 0
    assert {operation["state"] for operation in list_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
    )} == {"pending_confirmation"}


def test_batch_decision_dependency_drift_rolls_back_earlier_children(db_path):
    turn_id = uuid4()
    first = execute(
        db_path,
        extraction(company="A", position="前端"),
        client_turn_id=turn_id,
        text="完成了 A 前端的一面",
        approve=False,
    )
    second = execute(
        db_path,
        extraction(company="B", position="后端"),
        client_turn_id=turn_id,
        text="完成了 B 后端的一面",
        approve=False,
    )
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'B', '后端', ?, ?)",
            (now_iso(), now_iso()),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="整批没有执行"):
        decide_review_record_operations_for_turn(
            db_path,
            "u1",
            turn_id,
            [
                {"operation_id": first["operation_id"], "action": "approve"},
                {"operation_id": second["operation_id"], "action": "approve"},
            ],
        )

    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM applications WHERE user_id = 'u1' AND company = 'A'",
    ) == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries WHERE user_id = 'u1'") == 0
    assert [operation["state"] for operation in list_review_record_operations_for_turn(
        db_path,
        "u1",
        turn_id,
    )] == ["pending_confirmation", "pending_confirmation"]


def test_same_turn_supports_fifty_independent_reviews_and_rejects_the_fifty_first(db_path):
    turn_id = uuid4()
    for index in range(MAX_REVIEW_RECORD_OPERATIONS_PER_TURN):
        execute(
            db_path,
            extraction(company=f"公司{index:02d}", position=f"岗位{index:02d}"),
            client_turn_id=turn_id,
            text=f"投递公司{index:02d}的岗位{index:02d}",
            approve=False,
        )

    operations = list_review_record_operations_for_turn(db_path, "u1", turn_id)
    assert len(operations) == 50
    assert operations[0]["preview"]["extraction"]["company"] == "公司00"
    assert operations[-1]["preview"]["extraction"]["company"] == "公司49"
    with pytest.raises(ReviewRecordOperationConflict, match="达到安全上限"):
        execute(
            db_path,
            extraction(company="公司50", position="岗位50"),
            client_turn_id=turn_id,
            text="投递公司50的岗位50",
            approve=False,
        )


def test_processing_same_turn_replay_with_new_operation_id_reuses_first_owner(db_path):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        replay_extractor_calls = 0
        first_operation_id = uuid4()
        replay_operation_id = uuid4()
        turn_id = uuid4()
        text = "正在提取的一面复盘"

        async def blocked(_combined: str, _today: str):
            started.set()
            await release.wait()
            return extraction()

        async def must_not_extract(_combined: str, _today: str):
            nonlocal replay_extractor_calls
            replay_extractor_calls += 1
            raise AssertionError("processing replay must reuse the first owner")

        first_task = asyncio.create_task(execute_review_record_operation(
            db_path,
            "u1",
            operation_id=first_operation_id,
            client_turn_id=turn_id,
            text=text,
            review_reference=None,
            effective_date=TODAY,
            extractor=blocked,
        ))
        await started.wait()
        try:
            replay = await execute_review_record_operation(
                db_path,
                "u1",
                operation_id=replay_operation_id,
                client_turn_id=turn_id,
                text=text,
                review_reference=None,
                effective_date=TODAY,
                extractor=must_not_extract,
            )
            while_processing = (
                scalar(db_path, "SELECT COUNT(*) FROM journal"),
                scalar(db_path, "SELECT COUNT(*) FROM journal WHERE kind='review'"),
                scalar(
                    db_path,
                    "SELECT COUNT(*) FROM journal "
                    "WHERE kind='correction' AND operation_id IS NOT NULL",
                ),
                scalar(db_path, "SELECT COUNT(*) FROM applications"),
                scalar(db_path, "SELECT COUNT(*) FROM timeline_entries"),
                scalar(db_path, "SELECT COUNT(*) FROM status_log"),
            )
        finally:
            release.set()
            completed = await first_task
        return (
            first_operation_id,
            replay_operation_id,
            replay,
            completed,
            replay_extractor_calls,
            while_processing,
        )

    (
        first_operation_id,
        replay_operation_id,
        replay,
        completed,
        replay_extractor_calls,
        while_processing,
    ) = run(scenario())

    assert replay["operation_id"] == str(first_operation_id)
    assert replay["state"] == "processing" and replay["terminal"] is False
    assert replay_extractor_calls == 0
    assert while_processing == (2, 1, 1, 0, 0, 0)
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE operation_id = ?",
        str(replay_operation_id),
    ) == 0
    assert completed["operation_id"] == str(first_operation_id)
    assert completed["state"] == "pending_confirmation" and completed["terminal"] is False
    approved = approve_review_record_operation(db_path, "u1", first_operation_id)
    assert approved["state"] == "completed" and approved["terminal"] is True


@pytest.mark.parametrize("damage", [
    "kind",
    "extraction_family",
    "derivation_family",
    "both_family_unknown",
    "both_family_missing",
    "family_conflict",
    "extraction_turn",
    "derivation_turn",
])
def test_broad_locators_block_retry_before_writes_or_extractor(db_path, damage):
    operation_id = uuid4()
    turn_id = uuid4()
    execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        client_turn_id=turn_id,
    )
    with transaction(db_path) as conn:
        if damage == "kind":
            conn.execute(
                "UPDATE journal SET kind='review' WHERE operation_id=?",
                (str(operation_id),),
            )
        elif damage == "both_family_unknown":
            conn.execute(
                "UPDATE journal SET "
                "extraction_json=json_set(extraction_json, '$.operation_type', 'damaged'), "
                "derivation_json=json_set(derivation_json, '$.operation.type', 'damaged') "
                "WHERE operation_id=?",
                (str(operation_id),),
            )
        elif damage == "both_family_missing":
            conn.execute(
                "UPDATE journal SET "
                "extraction_json=json_remove(extraction_json, '$.operation_type'), "
                "derivation_json=json_remove(derivation_json, '$.operation.type') "
                "WHERE operation_id=?",
                (str(operation_id),),
            )
        elif damage == "family_conflict":
            conn.execute(
                "UPDATE journal SET derivation_json=json_set("
                "derivation_json, '$.operation.type', 'application_update') "
                "WHERE operation_id=?",
                (str(operation_id),),
            )
        else:
            column, path = {
                "extraction_family": ("extraction_json", "$.operation_type"),
                "derivation_family": ("derivation_json", "$.operation.type"),
                "extraction_turn": ("extraction_json", "$.client_turn_id"),
                "derivation_turn": (
                    "derivation_json",
                    "$.operation.client_turn_id",
                ),
            }[damage]
            conn.execute(
                f"UPDATE journal SET {column}=json_set({column}, ?, ?) "
                "WHERE operation_id=?",
                (path, str(uuid4()), str(operation_id)),
            )

    before_counts = (
        scalar(db_path, "SELECT COUNT(*) FROM journal"),
        scalar(db_path, "SELECT COUNT(*) FROM applications"),
        scalar(db_path, "SELECT COUNT(*) FROM timeline_entries"),
        scalar(db_path, "SELECT COUNT(*) FROM status_log"),
    )
    extractor_calls = 0

    async def must_not_extract(_combined: str, _today: str):
        nonlocal extractor_calls
        extractor_calls += 1
        return extraction()

    with pytest.raises(ReviewRecordOperationConflict):
        get_review_record_operation(db_path, "u1", operation_id)
    with pytest.raises(ReviewRecordOperationConflict):
        list_review_record_operations_for_turn(db_path, "u1", turn_id)
    with pytest.raises(ReviewRecordOperationConflict):
        execute(
            db_path,
            extraction(),
            operation_id=uuid4(),
            client_turn_id=turn_id,
            extractor=must_not_extract,
        )
    assert extractor_calls == 0
    assert (
        scalar(db_path, "SELECT COUNT(*) FROM journal"),
        scalar(db_path, "SELECT COUNT(*) FROM applications"),
        scalar(db_path, "SELECT COUNT(*) FROM timeline_entries"),
        scalar(db_path, "SELECT COUNT(*) FROM status_log"),
    ) == before_counts


def test_turn_locator_union_deduplicates_before_family_validation(db_path):
    turn_id = uuid4()
    result = execute(db_path, extraction(), client_turn_id=turn_id)
    with read_connection(db_path) as conn:
        rows = record_operation_runtime._operation_rows_for_turn(
            conn,
            "u1",
            str(turn_id),
        )
    assert len(rows) == 1
    assert list_review_record_operations_for_turn(db_path, "u1", turn_id) == [result]


def test_turn_locator_candidate_budget_fails_closed(db_path):
    turn_id = str(uuid4())
    extraction_json = json.dumps({
        "operation_type": "application_update",
        "client_turn_id": turn_id,
    })
    derivation_json = json.dumps({"operation": {
        "type": "application_update",
        "client_turn_id": turn_id,
    }})
    with transaction(db_path) as conn:
        conn.executemany(
            "INSERT INTO journal "
            "(user_id, kind, content, created_time, extraction_json, derivation_json, "
            "state, operation_id) VALUES (?, 'correction', ?, ?, ?, ?, 'applied', ?)",
            [
                (
                    "u1",
                    f"other operation {index}",
                    now_iso(),
                    extraction_json,
                    derivation_json,
                    str(uuid4()),
                )
                for index in range(
                    record_operation_runtime.MAX_TURN_OPERATION_CANDIDATES + 1,
                )
            ],
        )

    with pytest.raises(ReviewRecordOperationConflict, match="候选超过安全上限"):
        list_review_record_operations_for_turn(db_path, "u1", turn_id)

    related_tables = (
        "journal",
        "applications",
        "timeline_entries",
        "status_log",
        "questions",
        "knowledge_points",
        "question_knowledge",
        "review_question_occurrences",
    )
    before_counts = tuple(
        scalar(db_path, f"SELECT COUNT(*) FROM {table}")
        for table in related_tables
    )
    extractor_calls = 0

    async def must_not_extract(_combined: str, _today: str):
        nonlocal extractor_calls
        extractor_calls += 1
        return extraction()

    with pytest.raises(ReviewRecordOperationConflict, match="候选超过安全上限"):
        execute(
            db_path,
            extraction(),
            operation_id=uuid4(),
            client_turn_id=turn_id,
            extractor=must_not_extract,
        )

    assert extractor_calls == 0
    assert tuple(
        scalar(db_path, f"SELECT COUNT(*) FROM {table}")
        for table in related_tables
    ) == before_counts


def test_turn_locator_ignores_explicit_known_other_family(db_path):
    turn_id = uuid4()
    execute_preference_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=turn_id,
        changes=[{"op": "set", "key": "城市", "value": "哥本哈根"}],
    )
    recorded = execute(db_path, extraction(), client_turn_id=turn_id)
    assert list_review_record_operations_for_turn(db_path, "u1", turn_id) == [recorded]


@pytest.mark.parametrize("damage", [
    "kind",
    "extraction_turn",
    "derivation_turn",
    "derivation_root",
    "dual_family_keys",
])
def test_corrupted_other_immediate_family_blocks_record_write(db_path, damage):
    turn_id = uuid4()
    preference_operation = execute_preference_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=turn_id,
        changes=[{"op": "set", "key": "城市", "value": "哥本哈根"}],
    )
    with transaction(db_path) as conn:
        if damage == "kind":
            conn.execute(
                "UPDATE journal SET kind='review' WHERE operation_id=?",
                (preference_operation["operation_id"],),
            )
        elif damage == "derivation_root":
            conn.execute(
                "UPDATE journal SET derivation_json=json_set("
                "derivation_json, '$.unexpected', 1) WHERE operation_id=?",
                (preference_operation["operation_id"],),
            )
        elif damage == "dual_family_keys":
            conn.execute(
                "UPDATE journal SET derivation_json=json_set("
                "derivation_json, '$.operation.type', 'preference_update') "
                "WHERE operation_id=?",
                (preference_operation["operation_id"],),
            )
        else:
            column, path = {
                "extraction_turn": ("extraction_json", "$.client_turn_id"),
                "derivation_turn": (
                    "derivation_json",
                    "$.operation.client_turn_id",
                ),
            }[damage]
            conn.execute(
                f"UPDATE journal SET {column}=json_set({column}, ?, ?) "
                "WHERE operation_id=?",
                (path, str(uuid4()), preference_operation["operation_id"]),
            )

    before_journal = scalar(db_path, "SELECT COUNT(*) FROM journal")
    extractor_calls = 0

    async def must_not_extract(_combined: str, _today: str):
        nonlocal extractor_calls
        extractor_calls += 1
        return extraction()

    with pytest.raises(ReviewRecordOperationConflict):
        list_review_record_operations_for_turn(db_path, "u1", turn_id)
    with pytest.raises(ReviewRecordOperationConflict):
        execute(
            db_path,
            extraction(),
            client_turn_id=turn_id,
            extractor=must_not_extract,
        )
    assert extractor_calls == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM journal") == before_journal
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_large_valid_application_update_marker_is_not_rejected(db_path):
    turn_id = str(uuid4())
    extraction_json = json.dumps({
        "operation_type": "application_update",
        "client_turn_id": turn_id,
        "bounded_payload": "x" * (
            record_operation_runtime.MAX_REVIEW_RECORD_PERSISTED_JSON_CHARS + 10_000
        ),
    })
    assert (
        record_operation_runtime.MAX_REVIEW_RECORD_PERSISTED_JSON_CHARS
        < len(extraction_json)
        < record_operation_runtime.MAX_TURN_OPERATION_JSON_CHARS
    )
    derivation_json = json.dumps({"operation": {
        "type": "application_update",
        "client_turn_id": turn_id,
    }})
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO journal "
            "(user_id, kind, content, created_time, extraction_json, derivation_json, "
            "state, operation_id) VALUES (?, 'correction', ?, ?, ?, ?, 'applied', ?)",
            (
                "u1",
                "large application update marker",
                now_iso(),
                extraction_json,
                derivation_json,
                str(uuid4()),
            ),
        )

    recorded = execute(db_path, extraction(), client_turn_id=turn_id)
    assert list_review_record_operations_for_turn(db_path, "u1", turn_id) == [recorded]


def test_broad_locators_remain_tenant_scoped_and_collision_is_zero_write(db_path):
    operation_id = uuid4()
    turn_id = uuid4()
    execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        client_turn_id=turn_id,
    )
    before_journal = scalar(db_path, "SELECT COUNT(*) FROM journal")
    extractor_calls = 0

    async def must_not_extract(_combined: str, _today: str):
        nonlocal extractor_calls
        extractor_calls += 1
        return extraction()

    assert get_review_record_operation(db_path, "u2", operation_id) is None
    assert list_review_record_operations_for_turn(db_path, "u2", turn_id) == []
    with pytest.raises(ReviewRecordOperationNotFound):
        execute(
            db_path,
            extraction(),
            user_id="u2",
            operation_id=operation_id,
            client_turn_id=turn_id,
            extractor=must_not_extract,
        )
    assert extractor_calls == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM journal") == before_journal


def test_invalid_json_locator_error_does_not_retain_sensitive_receipt(db_path, caplog):
    sentinel = "PRIVATE-REVIEW-RECEIPT-SENTINEL"
    operation_id = uuid4()
    execute(db_path, extraction(), operation_id=operation_id)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET extraction_json=? WHERE operation_id=?",
            (sentinel, str(operation_id)),
        )

    with pytest.raises(ReviewRecordOperationConflict) as captured:
        get_review_record_operation(db_path, "u1", operation_id)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sentinel not in str(captured.value)
    assert sentinel not in caplog.text


def test_same_turn_replay_after_midnight_uses_first_frozen_effective_date(db_path):
    operation_id = uuid4()
    turn_id = uuid4()
    first = execute(
        db_path,
        extraction(),
        operation_id=operation_id,
        client_turn_id=turn_id,
    )

    async def must_not_extract(_combined: str, _today: str):
        raise AssertionError("a durable replay must not run extraction again")

    for replay_operation_id in (operation_id, uuid4()):
        replay = run(execute_review_record_operation(
            db_path,
            "u1",
            operation_id=replay_operation_id,
            client_turn_id=turn_id,
            text="今天完成了 A 的一面",
            review_reference=None,
            effective_date="2026-07-15",
            extractor=must_not_extract,
        ))
        assert replay == first
    assert scalar(
        db_path,
        "SELECT json_extract(extraction_json, '$.effective_date') "
        "FROM journal WHERE operation_id=?",
        str(operation_id),
    ) == TODAY


def test_clarification_and_opaque_supplement_publish_one_review(db_path):
    first_operation = uuid4()
    first = execute(
        db_path,
        extraction(company=None, position=None, step=None, occurred_date=None),
        operation_id=first_operation,
        text="今天面了一家公司",
    )
    assert first["state"] == "completed"
    assert first["outcome"] == "needs_clarification"
    assert {item["field"] for item in first["result"]["missing"]} == {"company"}
    assert list_pending_review_record_clarifications(db_path, "u1") == [first]

    supplement_operation = uuid4()
    supplemented = execute(
        db_path,
        extraction(),
        operation_id=supplement_operation,
        text="A 公司，一面，今天",
        review_reference=first_operation,
    )
    assert supplemented["state"] == "completed"
    assert supplemented["outcome"] == "applied"
    assert supplemented["review_reference"] == str(first_operation)
    assert supplemented["target_journal_id"] == first["target_journal_id"]
    assert supplemented["source_journal_id"] != supplemented["target_journal_id"]
    assert list_pending_review_record_clarifications(db_path, "u1") == []
    assert scalar(db_path, "SELECT COUNT(*) FROM journal WHERE kind='review'") == 1
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id IS NULL",
    ) == 1
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' AND operation_id IS NOT NULL",
    ) == 2
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1


def test_exact_pending_review_cannot_be_recreated_without_its_reference(db_path):
    text = "今天面了一家公司"
    first = execute(
        db_path,
        extraction(company=None, position=None, occurred_date=None),
        text=text,
    )
    assert first["outcome"] == "needs_clarification"

    with pytest.raises(ReviewRecordOperationConflict, match="相同原文已有一条复盘草稿"):
        execute(db_path, extraction(), text=text)

    assert scalar(db_path, "SELECT COUNT(*) FROM journal WHERE kind='review'") == 1


def test_pending_query_filters_ordinary_successes_before_its_safety_limit(db_path):
    pending = execute(
        db_path,
        extraction(company=None, position=None, step=None, occurred_date=None),
        text="缺少关键信息的复盘",
    )
    minimal = extraction()
    minimal.update({
        "questions": [],
        "mood": None,
        "time_of_day": None,
        "factors": [],
    })
    for index in range(MAX_PENDING_REVIEW_RECORD_CLARIFICATIONS + 1):
        applied = execute(db_path, minimal, text=f"普通成功复盘 {index}")
        assert applied["outcome"] == "applied"

    assert list_pending_review_record_clarifications(db_path, "u1") == [pending]


@pytest.mark.parametrize(
    ("column", "path"),
    [
        ("extraction_json", "$.operation_type"),
        ("derivation_json", "$.operation.result.outcome"),
    ],
)
def test_pending_query_fails_closed_when_family_or_outcome_is_damaged(
    db_path,
    column,
    path,
):
    pending = execute(
        db_path,
        extraction(company=None, position=None, step=None, occurred_date=None),
        text="稍后补充信息",
    )
    assert list_pending_review_record_clarifications(db_path, "u1") == [pending]
    with transaction(db_path) as conn:
        conn.execute(
            f"UPDATE journal SET {column}=json_set({column}, ?, 'damaged') "
            "WHERE operation_id=?",
            (path, pending["operation_id"]),
        )

    with pytest.raises(ReviewRecordOperationConflict):
        list_pending_review_record_clarifications(db_path, "u1")


def test_pending_query_hides_old_clarification_while_supplement_is_processing(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None),
        text="还没说公司",
    )

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_combined: str, _today: str):
            started.set()
            await release.wait()
            return extraction()

        task = asyncio.create_task(execute_review_record_operation(
            db_path,
            "u1",
            operation_id=uuid4(),
            client_turn_id=uuid4(),
            text="补充 A 公司",
            review_reference=initial["review_reference"],
            effective_date=TODAY,
            extractor=blocked,
        ))
        await started.wait()
        pending_while_running = list_pending_review_record_clarifications(db_path, "u1")
        release.set()
        completed = await task
        return pending_while_running, completed

    pending_while_running, completed = run(scenario())
    assert pending_while_running == []
    assert completed["state"] == "pending_confirmation"
    approved = approve_review_record_operation(db_path, "u1", completed["operation_id"])
    assert approved["state"] == "completed" and approved["outcome"] == "applied"


def test_initial_source_tamper_fails_closed_on_every_read_and_replay(db_path):
    operation_id = uuid4()
    turn_id = uuid4()
    original_text = "一条稍后需要补充的复盘"
    result = execute(
        db_path,
        extraction(company=None, position=None, step=None, occurred_date=None),
        operation_id=operation_id,
        client_turn_id=turn_id,
        text=original_text,
    )
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET content='被篡改的原文' WHERE id=?",
            (result["source_journal_id"],),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        get_review_record_operation(db_path, "u1", operation_id)
    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        list_review_record_operations_for_turn(db_path, "u1", turn_id)
    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        list_pending_review_record_clarifications(db_path, "u1")
    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        execute(
            db_path,
            extraction(),
            operation_id=operation_id,
            client_turn_id=turn_id,
            text=original_text,
        )


def test_supplement_source_tamper_fails_closed_on_every_read_and_replay(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None, step=None, occurred_date=None),
        text="初始信息不足",
    )
    operation_id = uuid4()
    turn_id = uuid4()
    supplement_text = "只补充公司，其他信息稍后再说"
    supplement = execute(
        db_path,
        extraction(company=None, position=None, step=None, occurred_date=None),
        operation_id=operation_id,
        client_turn_id=turn_id,
        text=supplement_text,
        review_reference=initial["review_reference"],
    )
    assert supplement["outcome"] == "needs_clarification"
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET content='被篡改的补充原文' WHERE id=?",
            (supplement["source_journal_id"],),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        get_review_record_operation(db_path, "u1", operation_id)
    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        list_review_record_operations_for_turn(db_path, "u1", turn_id)
    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        list_pending_review_record_clarifications(db_path, "u1")
    with pytest.raises(ReviewRecordOperationConflict, match="原文快照"):
        execute(
            db_path,
            extraction(),
            operation_id=operation_id,
            client_turn_id=turn_id,
            text=supplement_text,
            review_reference=initial["review_reference"],
        )


def test_later_applied_receipt_validates_its_frozen_supplement_prefix(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None),
        text="初始信息不足",
    )
    first_supplement = execute(
        db_path,
        extraction(company=None, position=None),
        text="第一次补充仍没说公司",
        review_reference=initial["review_reference"],
    )
    latest_operation_id = uuid4()
    latest_turn_id = uuid4()
    latest_text = "第二次补充确认 A 公司 P 岗"
    latest = execute(
        db_path,
        extraction(),
        operation_id=latest_operation_id,
        client_turn_id=latest_turn_id,
        text=latest_text,
        review_reference=initial["review_reference"],
    )
    assert latest["outcome"] == "applied"
    historical = get_review_record_operation(
        db_path,
        "u1",
        first_supplement["operation_id"],
    )
    assert historical is not None
    assert historical["operation_id"] == first_supplement["operation_id"]
    assert historical["result"] == first_supplement["result"]
    assert historical["target_current_state"] == "applied"

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET content='被篡改的较早补充' WHERE id=?",
            (first_supplement["source_journal_id"],),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="冻结的原文前缀"):
        get_review_record_operation(db_path, "u1", latest_operation_id)
    with pytest.raises(ReviewRecordOperationConflict, match="冻结的原文前缀"):
        list_review_record_operations_for_turn(db_path, "u1", latest_turn_id)
    with pytest.raises(ReviewRecordOperationConflict, match="冻结的原文前缀"):
        execute(
            db_path,
            extraction(),
            operation_id=latest_operation_id,
            client_turn_id=latest_turn_id,
            text=latest_text,
            review_reference=initial["review_reference"],
        )


def test_pending_latest_clarification_validates_earlier_supplement_prefix(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None),
        text="初始信息不足",
    )
    first_supplement = execute(
        db_path,
        extraction(company=None, position=None),
        text="第一次补充仍然不完整",
        review_reference=initial["review_reference"],
    )
    latest = execute(
        db_path,
        extraction(company=None, position=None),
        text="第二次补充仍然不完整",
        review_reference=initial["review_reference"],
    )
    assert list_pending_review_record_clarifications(db_path, "u1") == [latest]

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET content='被篡改的较早补充' WHERE id=?",
            (first_supplement["source_journal_id"],),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="冻结的原文前缀"):
        list_pending_review_record_clarifications(db_path, "u1")


def test_supplement_reference_is_tenant_scoped(db_path):
    first = execute(
        db_path,
        extraction(company=None),
        user_id="u1",
    )
    before = scalar(db_path, "SELECT COUNT(*) FROM journal")
    with pytest.raises(ReviewRecordOperationNotFound):
        execute(
            db_path,
            extraction(),
            user_id="u2",
            review_reference=first["review_reference"],
        )
    assert scalar(db_path, "SELECT COUNT(*) FROM journal") == before
    assert get_review_record_operation(db_path, "u2", first["operation_id"]) is None


def test_extractor_failure_is_terminal_and_has_no_business_projection(db_path):
    operation_id = uuid4()

    async def failing(_combined: str, _today: str):
        raise RuntimeError("provider secret must not persist")

    with pytest.raises(RuntimeError, match="provider secret"):
        execute(
            db_path,
            extraction(),
            operation_id=operation_id,
            extractor=failing,
        )
    operation = get_review_record_operation(db_path, "u1", operation_id)
    assert operation is not None and operation["state"] == "failed"
    assert operation["error"]["code"] == "extract_failed"
    assert "provider secret" not in operation["error"]["message"]
    assert scalar(db_path, "SELECT state FROM journal WHERE kind='review'") == "failed"
    for table in ("applications", "timeline_entries", "questions", "knowledge_points", "status_log"):
        assert scalar(db_path, f"SELECT COUNT(*) FROM {table}") == 0


def test_projection_or_receipt_failure_rolls_back_all_business_sinks(db_path):
    operation_id = uuid4()
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_record_event BEFORE INSERT ON timeline_entries "
            "BEGIN SELECT RAISE(ABORT, 'event unavailable'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="event unavailable"):
        execute(db_path, extraction(), operation_id=operation_id)

    operation = get_review_record_operation(db_path, "u1", operation_id)
    assert operation is not None and operation["state"] == "pending_confirmation"
    assert scalar(db_path, "SELECT state FROM journal WHERE kind='review'") == "pending"
    for table in ("companies", "applications", "timeline_entries", "questions", "knowledge_points"):
        assert scalar(db_path, f"SELECT COUNT(*) FROM {table}") == 0


def test_application_ambiguity_is_rechecked_after_extractor(db_path):
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'A', 'P1', ?, ?)",
            (now_iso(), now_iso()),
        )

    async def racing_extractor(_combined: str, _today: str):
        with transaction(db_path) as conn:
            conn.execute(
                "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
                "VALUES ('u1', 'A', 'P2', ?, ?)",
                (now_iso(), now_iso()),
            )
        return extraction(position=None)

    result = execute(
        db_path,
        extraction(position=None),
        extractor=racing_extractor,
    )
    assert result["outcome"] == "needs_clarification"
    assert [item["field"] for item in result["result"]["missing"]] == ["position"]
    assert scalar(db_path, "SELECT COUNT(*) FROM applications") == 2
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM applications WHERE position='未注明岗位'",
    ) == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0


def test_reversed_supplements_publish_only_latest_revision(db_path):
    initial = execute(
        db_path,
        extraction(company=None, position=None),
        text="面完了但没说公司",
    )

    async def scenario():
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_combined: list[str] = []

        async def first_extractor(_combined: str, _today: str):
            first_started.set()
            await release_first.wait()
            return extraction(step="一面")

        async def second_extractor(combined: str, _today: str):
            second_combined.append(combined)
            return extraction(step="二面")

        first_task = asyncio.create_task(execute_review_record_operation(
            db_path,
            "u1",
            operation_id=uuid4(),
            client_turn_id=uuid4(),
            text="第一次补充：A 公司",
            review_reference=initial["review_reference"],
            effective_date=TODAY,
            extractor=first_extractor,
        ))
        await first_started.wait()
        latest = await execute_review_record_operation(
            db_path,
            "u1",
            operation_id=uuid4(),
            client_turn_id=uuid4(),
            text="第二次补充：确认是二面",
            review_reference=initial["review_reference"],
            effective_date=TODAY,
            extractor=second_extractor,
        )
        release_first.set()
        old = await first_task
        return old, latest, second_combined[0]

    old, latest, combined = run(scenario())
    assert old["state"] == "superseded"
    assert latest["state"] == "pending_confirmation"
    latest = approve_review_record_operation(db_path, "u1", latest["operation_id"])
    assert latest["state"] == "completed" and latest["outcome"] == "applied"
    assert "第一次补充" in combined and "第二次补充" in combined
    assert scalar(db_path, "SELECT step FROM timeline_entries") == "二面"
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1


def test_startup_recovery_closes_processing_owner_before_late_result(db_path):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_combined: str, _today: str):
            started.set()
            await release.wait()
            return extraction()

        operation_id = uuid4()
        task = asyncio.create_task(execute_review_record_operation(
            db_path,
            "u1",
            operation_id=operation_id,
            client_turn_id=uuid4(),
            text="会在重启时中断",
            review_reference=None,
            effective_date=TODAY,
            extractor=blocked,
        ))
        await started.wait()
        with transaction(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            recovered = recover_interrupted_review_record_operations_in_transaction(conn)
        release.set()
        late = await task
        return operation_id, recovered, late

    operation_id, recovered, late = run(scenario())
    assert recovered == {"operations": 1, "reviews": 1}
    assert late["state"] == "failed"
    assert late == get_review_record_operation(db_path, "u1", operation_id)
    assert scalar(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    assert scalar(db_path, "SELECT state FROM journal WHERE kind='review'") == "failed"


def test_startup_recovery_closes_owner_when_processing_receipt_is_damaged(db_path):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_combined: str, _today: str):
            started.set()
            await release.wait()
            return extraction()

        operation_id = uuid4()
        task = asyncio.create_task(execute_review_record_operation(
            db_path,
            "u1",
            operation_id=operation_id,
            client_turn_id=uuid4(),
            text="processing envelope 会损坏",
            review_reference=None,
            effective_date=TODAY,
            extractor=blocked,
        ))
        await started.wait()
        with transaction(db_path) as conn:
            conn.execute(
                "UPDATE journal SET derivation_json='{}' WHERE operation_id=?",
                (str(operation_id),),
            )
        with transaction(db_path) as conn:
            recovered = recover_interrupted_review_record_operations_in_transaction(conn)
        release.set()
        late = await task
        return operation_id, recovered, late

    operation_id, recovered, late = run(scenario())
    assert recovered == {"operations": 1, "reviews": 1}
    assert late["state"] == "failed" and late["error"]["code"] == "interrupted"
    assert late == get_review_record_operation(db_path, "u1", operation_id)
    assert scalar(db_path, "SELECT state FROM journal WHERE kind='review'") == "failed"


def test_startup_recovery_preserves_turn_locator_when_proposal_is_damaged(db_path):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_combined: str, _today: str):
            started.set()
            await release.wait()
            return extraction()

        operation_id = uuid4()
        turn_id = uuid4()
        task = asyncio.create_task(execute_review_record_operation(
            db_path,
            "u1",
            operation_id=operation_id,
            client_turn_id=turn_id,
            text="proposal 会在重启前损坏",
            review_reference=None,
            effective_date=TODAY,
            extractor=blocked,
        ))
        await started.wait()
        with transaction(db_path) as conn:
            conn.execute(
                "UPDATE journal SET extraction_json=json_set(extraction_json, "
                "'$.operation_type', 'damaged') WHERE operation_id=?",
                (str(operation_id),),
            )
        with transaction(db_path) as conn:
            recovered = recover_interrupted_review_record_operations_in_transaction(conn)
        with read_connection(db_path) as conn:
            terminal_turn_id = conn.execute(
                "SELECT json_extract(derivation_json, '$.operation.client_turn_id') "
                "FROM journal WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()[0]
        release.set()
        with pytest.raises(ReviewRecordOperationConflict):
            await task
        return operation_id, turn_id, recovered, terminal_turn_id

    operation_id, turn_id, recovered, terminal_turn_id = run(scenario())
    assert recovered == {"operations": 1, "reviews": 1}
    assert terminal_turn_id == str(turn_id)
    with pytest.raises(ReviewRecordOperationConflict):
        list_review_record_operations_for_turn(db_path, "u1", turn_id)
    with pytest.raises(ReviewRecordOperationConflict):
        execute(
            db_path,
            extraction(),
            operation_id=uuid4(),
            client_turn_id=turn_id,
        )
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' "
        "AND operation_id IS NOT NULL",
    ) == 1
    assert scalar(
        db_path,
        "SELECT state FROM journal WHERE operation_id=?",
        str(operation_id),
    ) == "failed"


def test_record_receipt_cannot_prepare_undo_after_review_revision_drift(db_path):
    recorded = execute(db_path, extraction())
    execute_review_timeline_entry_edit_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company="A",
        position="P",
        changes={"step": "二面"},
    )
    current = get_review_record_operation(db_path, "u1", recorded["operation_id"])
    assert current is not None
    assert current["target_current_state"] == "applied"
    assert current["target_current_revision"] > current["result"]["target_revision"]
    assert current["undo_available"] is False
    assert current["undo_block_reason"] == "target_changed"

    with pytest.raises(ReviewRecordOperationConflict, match="已被修正或撤销"):
        prepare_review_record_undo_operation(
            db_path,
            "u1",
            recorded["operation_id"],
        )
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='correction' "
        "AND json_extract(extraction_json, '$.operation_type')='review_undo'",
    ) == 0


def test_record_receipt_tracks_target_after_approved_review_undo(db_path):
    recorded = execute(db_path, extraction())
    prepared = prepare_review_record_undo_operation(
        db_path,
        "u1",
        recorded["operation_id"],
    )

    approve_review_operation(db_path, "u1", prepared["operation_id"])
    current = get_review_record_operation(db_path, "u1", recorded["operation_id"])

    assert current is not None
    assert current["target_current_state"] == "voided"
    assert current["target_current_revision"] > current["result"]["target_revision"]
    assert current["undo_available"] is False
    assert current["undo_block_reason"] == "target_not_applied"


def test_corrupt_persisted_contract_fails_closed(db_path):
    result = execute(db_path, extraction())
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE journal SET extraction_json='{}' WHERE operation_id=?",
            (result["operation_id"],),
        )
    with pytest.raises(ReviewRecordOperationConflict):
        get_review_record_operation(db_path, "u1", result["operation_id"])


def test_coercible_nested_receipt_tamper_fails_closed(db_path):
    result = execute(db_path, extraction())
    with transaction(db_path) as conn:
        raw = conn.execute(
            "SELECT derivation_json FROM journal WHERE operation_id=?",
            (result["operation_id"],),
        ).fetchone()[0]
        receipt = json.loads(raw)
        receipt["operation"]["result"]["extraction"]["history"]["step"] = 1
        conn.execute(
            "UPDATE journal SET derivation_json=? WHERE operation_id=?",
            (json.dumps(receipt, ensure_ascii=False), result["operation_id"]),
        )

    with pytest.raises(ReviewRecordOperationConflict, match="回执已损坏"):
        get_review_record_operation(db_path, "u1", result["operation_id"])


def test_input_bounds_reject_before_any_journal_write(db_path):
    with pytest.raises(ValueError, match="不能为空"):
        execute(db_path, extraction(), text="   ")
    with pytest.raises(ValueError, match="不能超过"):
        execute(db_path, extraction(), text="x" * (MAX_REVIEW_RECORD_SOURCE_CHARS + 1))
    with pytest.raises(ValueError, match="text 必须是字符串"):
        execute(db_path, extraction(), text=True)  # type: ignore[arg-type]
    assert scalar(db_path, "SELECT COUNT(*) FROM journal") == 0


def test_http_recovery_pending_and_prepare_undo_contract(client):
    test_client, db_path = client
    applied_operation = uuid4()
    applied_turn = uuid4()
    applied = execute(
        db_path,
        extraction(),
        user_id="me",
        operation_id=applied_operation,
        client_turn_id=applied_turn,
    )
    clarification = execute(
        db_path,
        extraction(company=None, position=None),
        user_id="me",
    )
    base = f"/api/reviews/record-operations/{applied_operation}"

    assert test_client.get(base).json() == applied
    assert test_client.get(
        f"/api/reviews/record-operations/by-client-turn/{applied_turn}",
    ).json() == {"operations": [applied]}
    assert test_client.get(
        "/api/reviews/record-operations/pending-clarifications",
    ).json() == {"operations": [clarification]}
    assert test_client.get(base, headers={"Remote-User": "other"}).status_code == 404
    assert test_client.post(base + "/prepare-undo").status_code == 422
    assert test_client.post(
        base + "/prepare-undo",
        json={"confirmed": True},
    ).status_code == 422

    prepared = test_client.post(base + "/prepare-undo", json={})
    assert prepared.status_code == 200
    assert prepared.json()["state"] == "pending"
    assert prepared.json()["target"]["journal_id"] == applied["target_journal_id"]
    assert prepared.json()["operation_id"] != applied["operation_id"]


def test_http_by_turn_recovers_three_pending_review_proposals(client):
    test_client, db_path = client
    turn_id = uuid4()
    operations = [
        execute(
            db_path,
            extraction(company=company, position=position, step=step),
            user_id="me",
            client_turn_id=turn_id,
            text=text,
            approve=False,
        )
        for company, position, step, text in [
            ("重庆理工大学", "助教", None, "投递重庆理工大学助教"),
            ("重庆大学", "助理教授", None, "投递重庆大学助理教授"),
            ("交通大学", "前端工程师", "二面", "交通大学前端工程师进入二面"),
        ]
    ]

    response = test_client.get(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}",
    )

    assert response.status_code == 200
    assert response.json() == {"operations": operations}


def test_http_by_turn_recovers_fifty_pending_review_proposals(client):
    test_client, db_path = client
    turn_id = uuid4()
    operations = [
        execute(
            db_path,
            extraction(company=f"公司{index:02d}", position=f"岗位{index:02d}"),
            user_id="me",
            client_turn_id=turn_id,
            text=f"投递公司{index:02d}的岗位{index:02d}",
            approve=False,
        )
        for index in range(MAX_REVIEW_RECORD_OPERATIONS_PER_TURN)
    ]

    response = test_client.get(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}",
    )

    assert response.status_code == 200
    assert response.json() == {"operations": operations}
    decided = test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json={"decisions": [
            {"operation_id": operation["operation_id"], "action": "reject"}
            for operation in operations
        ]},
    )
    assert decided.status_code == 200
    assert len(decided.json()["operations"]) == MAX_REVIEW_RECORD_OPERATIONS_PER_TURN
    assert {item["state"] for item in decided.json()["operations"]} == {"rejected"}


def test_http_pending_confirmation_approve_and_reject_contract(client):
    test_client, db_path = client
    approve_id = uuid4()
    pending = execute(
        db_path,
        extraction(),
        user_id="me",
        operation_id=approve_id,
        approve=False,
    )
    reject_id = uuid4()
    rejected_pending = execute(
        db_path,
        extraction(company="B", position="P"),
        user_id="me",
        operation_id=reject_id,
        text="B 公司复盘",
        approve=False,
    )

    listed = test_client.get("/api/reviews/record-operations/pending-confirmations")
    assert listed.status_code == 200
    assert {item["operation_id"] for item in listed.json()["operations"]} == {
        pending["operation_id"],
        rejected_pending["operation_id"],
    }
    assert test_client.post(
        f"/api/reviews/record-operations/{approve_id}/approve",
    ).status_code == 422
    approved = test_client.post(
        f"/api/reviews/record-operations/{approve_id}/approve",
        json={},
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "completed"

    rejected = test_client.post(
        f"/api/reviews/record-operations/{reject_id}/reject",
        json={},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    assert test_client.get(
        "/api/reviews/record-operations/pending-confirmations",
    ).json() == {"operations": []}


def test_http_review_batch_decision_is_one_strict_command(client):
    test_client, db_path = client
    turn_id = uuid4()
    staged = [
        execute(
            db_path,
            extraction(company=company, position=position),
            user_id="me",
            client_turn_id=turn_id,
            text=f"完成了 {company} {position} 的一面",
            approve=False,
        )
        for company, position in [("A", "前端"), ("B", "后端"), ("C", "算法")]
    ]
    payload = {"decisions": [
        {"operation_id": staged[0]["operation_id"], "action": "approve"},
        {"operation_id": staged[1]["operation_id"], "action": "reject"},
        {"operation_id": staged[2]["operation_id"], "action": "approve"},
    ]}

    assert test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json={"decisions": [payload["decisions"][0]]},
    ).status_code == 409

    response = test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json=payload,
    )

    assert response.status_code == 200
    assert [item["state"] for item in response.json()["operations"]] == [
        "completed", "rejected", "completed",
    ]
    assert test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json=payload,
    ).json() == response.json()
    assert test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json={"decisions": [payload["decisions"][0], payload["decisions"][0]]},
    ).status_code == 422
    assert test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json={**payload, "unexpected": True},
    ).status_code == 422


def test_http_review_batch_edit_rejects_coercion_and_aggregate_oversize(client):
    test_client, _ = client
    turn_id = uuid4()
    strict_edit = {
        **extraction(),
        "history": {**extraction()["history"], "step": 2},
    }
    assert test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json={"decisions": [{
            "operation_id": str(uuid4()),
            "action": "approve",
            "edited_extraction": strict_edit,
        }]},
    ).status_code == 422

    large_questions = [{
        "text": f"{index:02d}" + ("问" * 998),
        "stuck": False,
        "knowledge_points": [],
    } for index in range(45)]
    large_edit = {
        **extraction(),
        "questions": large_questions,
    }
    assert test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json={"decisions": [{
            "operation_id": str(uuid4()),
            "action": "approve",
            "edited_extraction": large_edit,
        } for _ in range(12)]},
    ).status_code == 422


def test_http_review_batch_edit_rejects_noop_without_business_writes(client):
    test_client, db_path = client
    execute(db_path, extraction(), user_id="me")
    turn_id = uuid4()
    staged = execute(
        db_path,
        extraction(step="二面", current_step="二面"),
        user_id="me",
        client_turn_id=turn_id,
        text="A P 已完成二面",
        approve=False,
    )
    before = (
        scalar(db_path, "SELECT revision FROM applications WHERE user_id='me'"),
        scalar(db_path, "SELECT COUNT(*) FROM timeline_entries WHERE user_id='me'"),
        scalar(db_path, "SELECT COUNT(*) FROM status_log WHERE user_id='me'"),
    )
    no_op = {
        "company": "A",
        "position": "P",
        "channel": None,
        "history": None,
        "projected_state": {"stage": "interviewing", "current_step": "一面"},
        "next_action": None,
        "questions": [],
        "mood": None,
        "time_of_day": None,
        "factors": [],
    }

    response = test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{turn_id}/decide",
        json={"decisions": [{
            "operation_id": staged["operation_id"],
            "action": "approve",
            "edited_extraction": no_op,
        }]},
    )

    assert response.status_code == 409
    assert "没有可发布的有效进展" in response.json()["detail"]
    assert before == (
        scalar(db_path, "SELECT revision FROM applications WHERE user_id='me'"),
        scalar(db_path, "SELECT COUNT(*) FROM timeline_entries WHERE user_id='me'"),
        scalar(db_path, "SELECT COUNT(*) FROM status_log WHERE user_id='me'"),
    )
    assert get_review_record_operation(
        db_path,
        "me",
        staged["operation_id"],
    )["state"] == "pending_confirmation"


def test_http_review_batch_edit_rejects_terminal_stage_with_next_action(client):
    test_client, _ = client
    terminal_with_plan = {
        **extraction(),
        "projected_state": {"stage": "rejected", "current_step": "已结束"},
        "next_action": {
            "stage": "interviewing",
            "step": "二面",
            "date": "2026-07-20",
            "time": None,
            "note": None,
        },
    }

    response = test_client.post(
        f"/api/reviews/record-operations/by-client-turn/{uuid4()}/decide",
        json={"decisions": [{
            "operation_id": str(uuid4()),
            "action": "approve",
            "edited_extraction": terminal_with_plan,
        }]},
    )

    assert response.status_code == 422


def test_http_record_contract_publishes_versioned_openapi_models(client):
    test_client, _ = client
    openapi = test_client.app.openapi()
    paths = openapi["paths"]

    assert paths[
        "/api/reviews/record-operations/by-client-turn/{client_turn_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReviewRecordOperationsResponse",
    }
    assert paths[
        "/api/reviews/record-operations/by-client-turn/{client_turn_id}/decide"
    ]["post"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReviewRecordOperationsResponse",
    }
    assert paths[
        "/api/reviews/record-operations/pending-clarifications"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PendingReviewRecordClarificationsResponse",
    }
    assert paths[
        "/api/reviews/record-operations/pending-confirmations"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PendingReviewRecordConfirmationsResponse",
    }
    assert paths[
        "/api/reviews/record-operations/{operation_id}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReviewRecordOperationDTO",
    }
    operation_schema = openapi["components"]["schemas"]["ReviewRecordOperationDTO"]
    assert operation_schema["properties"]["contract_version"]["const"] == 1
    assert operation_schema["additionalProperties"] is False
    for action in ("approve", "reject"):
        assert paths[
            f"/api/reviews/record-operations/{{operation_id}}/{action}"
        ]["post"]["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ReviewRecordOperationDTO",
        }
    command_schema = openapi["components"]["schemas"]["ReviewOperationCommand"]
    assert command_schema["additionalProperties"] is False
    batch_command_schema = openapi["components"]["schemas"][
        "ReviewRecordBatchDecisionRequest"
    ]
    assert batch_command_schema["additionalProperties"] is False
    assert batch_command_schema["properties"]["decisions"]["minItems"] == 1
    assert batch_command_schema["properties"]["decisions"]["maxItems"] == 50
