"""Timeline user-interaction regressions for the clean stage/step/next-action model."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from careerdesk.agentic.tools.manage_timeline import UpdateApplicationTool
from careerdesk.core.config import get_settings
from careerdesk.features.applications import repository as application_repository
from careerdesk.features.applications.operations import undo_application_update_operation
from careerdesk.platform.database import now_iso, read_connection, transaction


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Shanghai")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client, str(tmp_path / "careerdesk.db")
    get_settings.cache_clear()


def insert_application(
    db_path: str,
    *,
    stage: str = "backlog",
    current_step: str | None = None,
    next_action: dict | None = None,
    note: str | None = None,
) -> int:
    timestamp = now_iso()
    next_action = next_action or {}
    with transaction(db_path) as conn:
        return int(conn.execute(
            "INSERT INTO applications (user_id, company, position, stage, current_step, "
            "next_stage, next_step, next_date, next_time, next_note, application_note, "
            "created_time, updated_time) VALUES "
            "('me', '测试公司', '测试岗位', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage,
                current_step,
                next_action.get("stage"),
                next_action.get("step"),
                next_action.get("date"),
                next_action.get("time"),
                next_action.get("note"),
                note,
                timestamp,
                timestamp,
            ),
        ).lastrowid)


def scalar(db_path: str, sql: str, *parameters):
    with read_connection(db_path) as conn:
        return conn.execute(sql, parameters).fetchone()[0]


def application_path(application_id: int) -> str:
    return f"/api/timeline/applications/{application_id}"


def last_entry(detail: dict) -> dict:
    return detail["timeline_entries"][-1]


def test_create_application_normalizes_projection_and_next_action(client):
    test_client, db_path = client
    created = test_client.post("/api/timeline/applications", json={
        "company": "  新公司  ",
        "position": "  平台工程师  ",
        "department": "  基础架构  ",
        "channel": "  内推  ",
        "stage": "backlog",
        "current_step": None,
        "next_action": {
            "stage": "applied",
            "step": "提交申请",
            "date": "2026-07-25",
            "time": "10:30",
            "note": "检查附件",
        },
        "jd_text": "  负责平台建设  ",
    })

    assert created.status_code == 201, created.text
    detail = created.json()
    assert {key: detail[key] for key in (
        "company", "position", "department", "channel", "stage", "current_step", "jd_text",
    )} == {
        "company": "新公司",
        "position": "平台工程师",
        "department": "基础架构",
        "channel": "内推",
        "stage": "backlog",
        "current_step": None,
        "jd_text": "负责平台建设",
    }
    assert detail["next_action"] == {
        "stage": "applied",
        "step": "提交申请",
        "date": "2026-07-25",
        "time": "10:30",
        "note": "检查附件",
    }
    assert detail["timeline_entries"] == []
    assert detail["revision"] == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM applications WHERE user_id='me'") == 1
    assert test_client.get("/api/timeline/board").json()["total"] == 1


def test_create_application_in_later_stage_records_a_factual_transition(client):
    test_client, _db_path = client
    created = test_client.post("/api/timeline/applications", json={
        "company": "面试公司",
        "position": "后端工程师",
        "stage": "interviewing",
        "current_step": "一面",
        "next_action": None,
    })

    assert created.status_code == 201, created.text
    detail = created.json()
    entry = last_entry(detail)
    assert (entry["from_stage"], entry["from_step"]) == ("backlog", None)
    assert (entry["to_stage"], entry["to_step"]) == ("interviewing", "一面")
    assert entry["step"] == "一面"
    assert entry["source"] == "manual"
    assert entry["occurred_date"] == datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert "新增岗位并设为「面试中」" in entry["summary"]


def test_create_application_rejects_duplicate_and_blank_identity(client):
    test_client, db_path = client
    payload = {"company": "重复公司", "position": "重复岗位", "stage": "backlog"}
    assert test_client.post("/api/timeline/applications", json=payload).status_code == 201
    duplicate = test_client.post("/api/timeline/applications", json=payload)
    assert duplicate.status_code == 409
    assert scalar(
        db_path,
        "SELECT COUNT(*) FROM applications WHERE user_id='me' AND company='重复公司' "
        "AND position='重复岗位'",
    ) == 1
    assert test_client.post("/api/timeline/applications", json={
        "company": "   ", "position": "岗位",
    }).status_code == 422


def test_stage_drag_is_one_revision_cas_and_preserves_current_step(client):
    test_client, db_path = client
    application_id = insert_application(db_path, stage="written_test", current_step="在线测评")
    endpoint = f"{application_path(application_id)}/stage"

    moved = test_client.put(endpoint, json={
        "expected_revision": 0,
        "stage": "interviewing",
        "origin": "board_drag",
    })
    assert moved.status_code == 200, moved.text
    detail = moved.json()
    assert (detail["stage"], detail["current_step"], detail["revision"]) == (
        "interviewing", "在线测评", 1,
    )
    entry = last_entry(detail)
    assert (entry["from_stage"], entry["to_stage"], entry["source"]) == (
        "written_test", "interviewing", "drag",
    )
    assert (entry["from_step"], entry["to_step"]) == ("在线测评", "在线测评")
    assert entry["occurred_date"] == datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    stale = test_client.put(endpoint, json={
        "expected_revision": 0, "stage": "offer", "origin": "board_drag",
    })
    assert stale.status_code == 409
    assert scalar(db_path, "SELECT stage FROM applications WHERE id=?", application_id) == "interviewing"
    assert scalar(
        db_path, "SELECT COUNT(*) FROM timeline_entries WHERE application_id=?", application_id,
    ) == 1


def test_detail_stage_menu_records_manual_adjustment_without_claiming_a_drag(client):
    test_client, db_path = client
    application_id = insert_application(db_path, stage="applied", current_step="简历筛选")

    moved = test_client.put(f"{application_path(application_id)}/stage", json={
        "expected_revision": 0,
        "stage": "pooled",
        "origin": "detail_menu",
    })

    assert moved.status_code == 200, moved.text
    entry = last_entry(moved.json())
    assert entry["source"] == "manual"
    assert entry["summary"] == "阶段从「已投递」调整为「泡池子」"
    assert scalar(db_path, "SELECT current_step FROM applications WHERE id=?", application_id) == "简历筛选"


def test_move_to_terminal_stage_clears_next_action_and_keeps_step(client):
    test_client, db_path = client
    application_id = insert_application(
        db_path,
        stage="interviewing",
        current_step="二面",
        next_action={
            "stage": "interviewing", "step": "三面", "date": "2026-07-28",
            "time": "09:00", "note": "准备系统设计",
        },
    )
    moved = test_client.put(f"{application_path(application_id)}/stage", json={
        "expected_revision": 0,
        "stage": "rejected",
        "origin": "board_drag",
    })
    assert moved.status_code == 200, moved.text
    detail = moved.json()
    assert detail["stage"] == "rejected"
    assert detail["current_step"] == "二面"
    assert detail["next_action"] is None
    assert last_entry(detail)["to_step"] == "二面"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT next_stage, next_step, next_date, next_time, next_note FROM applications "
            "WHERE id=?", (application_id,),
        ).fetchone() == (None, None, None, None, None)


def test_pooled_stage_retains_next_action_but_hides_upcoming(client):
    test_client, _db_path = client
    planned_date = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=7)).isoformat()
    application_id = insert_application(
        _db_path,
        stage="interviewing",
        current_step="二面",
        next_action={
            "stage": "interviewing", "step": "等待 HC", "date": planned_date,
            "time": None, "note": "招聘方暂缓",
        },
    )
    pooled = test_client.put(f"{application_path(application_id)}/stage", json={
        "expected_revision": 0,
        "stage": "pooled",
        "origin": "board_drag",
    })
    assert pooled.status_code == 200
    assert pooled.json()["next_action"]["step"] == "等待 HC"
    assert pooled.json()["paused_from_stage"] == "interviewing"
    upcoming_ids = {item["id"] for item in test_client.get(
        "/api/timeline/upcoming", params={"days": 60},
    ).json()["items"]}
    assert application_id not in upcoming_ids

    resumed = test_client.put(f"{application_path(application_id)}/stage", json={
        "expected_revision": 1,
        "stage": "interviewing",
        "origin": "board_drag",
    })
    assert resumed.status_code == 200
    assert resumed.json()["next_action"]["date"] == planned_date
    assert resumed.json()["paused_from_stage"] is None


def test_profile_is_a_required_full_snapshot_and_invalidates_only_prep_inputs(client):
    test_client, db_path = client
    application_id = insert_application(db_path, stage="applied")
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET department='旧部门', channel='旧渠道', jd_text='旧 JD', "
            "prep_status='ready', prep_json='{}' WHERE id=?",
            (application_id,),
        )
    endpoint = f"{application_path(application_id)}/profile"
    assert test_client.put(endpoint, json={
        "expected_revision": 0,
        "company": "测试公司",
        "position": "测试岗位",
    }).status_code == 422

    channel_only = test_client.put(endpoint, json={
        "expected_revision": 0,
        "company": "测试公司",
        "position": "测试岗位",
        "department": "旧部门",
        "channel": "内推",
        "stage": "applied",
        "current_step": None,
        "next_action": None,
        "jd_text": "旧 JD",
    })
    assert channel_only.status_code == 200, channel_only.text
    assert channel_only.json()["prep_status"] == "ready"

    changed_input = test_client.put(endpoint, json={
        "expected_revision": 1,
        "company": "新公司",
        "position": "新岗位",
        "department": None,
        "channel": "内推",
        "stage": "interviewing",
        "current_step": "一面",
        "next_action": {
            "stage": "interviewing", "step": "二面", "date": None, "time": None, "note": None,
        },
        "jd_text": "新 JD",
    })
    assert changed_input.status_code == 200, changed_input.text
    detail = changed_input.json()
    assert (detail["company"], detail["position"], detail["stage"], detail["current_step"]) == (
        "新公司", "新岗位", "interviewing", "一面",
    )
    assert detail["next_action"]["step"] == "二面"
    assert detail["prep_status"] == "none"
    assert detail["revision"] == 2
    assert last_entry(detail)["to_step"] == "一面"


def test_profile_to_terminal_stage_clears_next_action(client):
    test_client, db_path = client
    application_id = insert_application(
        db_path,
        stage="interviewing",
        current_step="终面",
        next_action={"stage": "offer", "step": "确认 offer", "date": None, "time": None, "note": None},
    )
    response = test_client.put(f"{application_path(application_id)}/profile", json={
        "expected_revision": 0,
        "company": "测试公司",
        "position": "测试岗位",
        "department": None,
        "channel": None,
        "stage": "withdrawn",
        "current_step": "终面",
        "next_action": None,
        "jd_text": None,
    })
    assert response.status_code == 200, response.text
    assert response.json()["next_action"] is None
    assert last_entry(response.json())["to_stage"] == "withdrawn"


def test_revision_rejects_stage_aba_even_when_stage_matches_again(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    with transaction(db_path) as conn:
        conn.execute("UPDATE applications SET stage='applied', revision=revision+1 WHERE id=?", (application_id,))
        conn.execute("UPDATE applications SET stage='backlog', revision=revision+1 WHERE id=?", (application_id,))
    stale = test_client.put(f"{application_path(application_id)}/stage", json={
        "expected_revision": 0,
        "stage": "offer",
        "origin": "board_drag",
    })
    assert stale.status_code == 409
    assert scalar(db_path, "SELECT stage FROM applications WHERE id=?", application_id) == "backlog"


def test_detail_delete_only_prepares_a_frozen_confirmation(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    prepared = test_client.post(f"{application_path(application_id)}/prepare-delete", json={})
    assert prepared.status_code == 200, prepared.text
    operation = prepared.json()
    assert operation["state"] == "pending"
    assert operation["target"]["application_id"] == application_id
    assert scalar(db_path, "SELECT COUNT(*) FROM applications WHERE id=?", application_id) == 1


def test_progress_rejects_date_only_and_accepts_a_fact_without_projection_change(client):
    test_client, _db_path = client
    application_id = insert_application(_db_path, stage="applied", current_step="已投递")
    endpoint = f"{application_path(application_id)}/progress"
    date_only = test_client.post(endpoint, json={
        "expected_revision": 0,
        "occurred_date": "2026-07-19",
        "update_current_state": False,
    })
    assert date_only.status_code == 422

    recorded = test_client.post(endpoint, json={
        "expected_revision": 0,
        "step": "联系 HR",
        "occurred_date": "2026-07-19",
        "outcome": None,
        "summary": "询问进度",
        "update_current_state": False,
    })
    assert recorded.status_code == 200, recorded.text
    detail = recorded.json()
    assert (detail["stage"], detail["current_step"]) == ("applied", "已投递")
    assert last_entry(detail)["step"] == "联系 HR"
    assert last_entry(detail)["from_stage"] == last_entry(detail)["to_stage"] == "applied"


def test_manual_history_crud_never_overloads_application_note(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    base = application_path(application_id)
    noted = test_client.put(f"{base}/note", json={
        "expected_revision": 0,
        "note": "岗位私有备注",
    })
    assert noted.status_code == 200
    created = test_client.post(f"{base}/progress", json={
        "expected_revision": 1,
        "step": "二面",
        "occurred_date": "2026-07-17",
        "outcome": "passed",
        "summary": "二面结束",
        "update_current_state": False,
    })
    assert created.status_code == 200, created.text
    entry = last_entry(created.json())

    updated = test_client.put(f"{base}/timeline-entries/{entry['id']}", json={
        "expected_revision": 2,
        "expected_fingerprint": entry["snapshot_fingerprint"],
        "step": "技术二面",
        "occurred_date": "2026-07-18",
        "outcome": "passed",
        "summary": "补充面试官反馈",
    })
    assert updated.status_code == 200, updated.text
    edited_entry = last_entry(updated.json())
    assert edited_entry["step"] == "技术二面"
    assert updated.json()["application_note"] == "岗位私有备注"

    deleted = test_client.delete(
        f"{base}/timeline-entries/{entry['id']}",
        params={
            "expected_revision": 3,
            "expected_fingerprint": edited_entry["snapshot_fingerprint"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["timeline_entries"] == []
    assert deleted.json()["application_note"] == "岗位私有备注"


def test_manual_history_rejects_stale_edit_and_delete_snapshots(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    base = application_path(application_id)
    created = test_client.post(f"{base}/progress", json={
        "expected_revision": 0,
        "step": "联系 HR",
        "summary": "等待反馈",
        "update_current_state": False,
    }).json()
    entry = last_entry(created)
    current = test_client.put(f"{base}/timeline-entries/{entry['id']}", json={
        "expected_revision": 1,
        "expected_fingerprint": entry["snapshot_fingerprint"],
        "step": "收到反馈",
        "occurred_date": None,
        "outcome": None,
        "summary": "进入下一轮",
    })
    assert current.status_code == 200
    stale_edit = test_client.put(f"{base}/timeline-entries/{entry['id']}", json={
        "expected_revision": 1,
        "expected_fingerprint": entry["snapshot_fingerprint"],
        "step": "旧窗口覆盖",
        "occurred_date": None,
        "outcome": None,
        "summary": None,
    })
    assert stale_edit.status_code == 409
    stale_delete = test_client.delete(
        f"{base}/timeline-entries/{entry['id']}",
        params={"expected_revision": 1, "expected_fingerprint": entry["snapshot_fingerprint"]},
    )
    assert stale_delete.status_code == 409
    assert scalar(db_path, "SELECT step FROM timeline_entries WHERE id=?", entry["id"]) == "收到反馈"


def test_note_revision_conflict_preserves_agent_append_then_accepts_rebase(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    endpoint = f"{application_path(application_id)}/note"
    first = test_client.put(endpoint, json={"expected_revision": 0, "note": "用户草稿起点"})
    assert first.status_code == 200
    appended = UpdateApplicationTool(
        db_path,
        "me",
        client_turn_id="00000000-0000-4000-8000-000000000501",
        local_date="2026-07-19",
    ).run({"updates": [{
        "company": "测试公司", "position": "测试岗位", "append_note": "Agent 新增信息",
    }]})
    assert appended.status == "success"
    stale = test_client.put(endpoint, json={"expected_revision": 1, "note": "旧窗口整段保存"})
    assert stale.status_code == 409
    authoritative = "用户草稿起点\nAgent 新增信息"
    assert application_repository.application_detail(
        db_path, "me", application_id,
    )["application_note"] == authoritative
    rebased = test_client.put(endpoint, json={
        "expected_revision": 2,
        "note": "用户合并后的最终内容",
    })
    assert rebased.status_code == 200
    assert rebased.json()["application_note"] == "用户合并后的最终内容"


def test_corrupt_timeline_timestamp_degrades_to_unknown_display_time(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    base = application_path(application_id)
    created = test_client.post(f"{base}/progress", json={
        "expected_revision": 0,
        "summary": "历史坏时间仍应可读",
        "update_current_state": False,
    })
    assert created.status_code == 200
    entry_id = last_entry(created.json())["id"]
    with transaction(db_path) as conn:
        conn.execute("UPDATE timeline_entries SET created_time='not-a-timestamp' WHERE id=?", (entry_id,))
    detail = test_client.get(base)
    assert detail.status_code == 200
    assert detail.json()["timeline_entries"][0]["display_time"] == "时间未知"


def test_complete_next_action_same_stage_advances_only_current_step(client):
    test_client, db_path = client
    application_id = insert_application(
        db_path,
        stage="interviewing",
        current_step="一面",
        next_action={
            "stage": "interviewing", "step": "二面", "date": "2026-07-19",
            "time": "14:00", "note": "准备项目复盘",
        },
    )
    completed = test_client.post(f"{application_path(application_id)}/complete-next-action", json={
        "expected_revision": 0,
        "occurred_date": "2026-07-19",
        "outcome": "passed",
        "summary": "二面完成",
        "next_action": {
            "stage": "interviewing", "step": "三面", "date": "2026-07-22",
            "time": None, "note": None,
        },
    })
    assert completed.status_code == 200, completed.text
    detail = completed.json()
    assert (detail["stage"], detail["current_step"]) == ("interviewing", "二面")
    assert detail["next_action"]["step"] == "三面"
    entry = last_entry(detail)
    assert (entry["from_stage"], entry["to_stage"]) == ("interviewing", "interviewing")
    assert (entry["from_step"], entry["to_step"]) == ("一面", "二面")


def test_complete_next_action_can_apply_its_explicit_target_stage(client):
    test_client, db_path = client
    application_id = insert_application(
        db_path,
        stage="written_test",
        current_step="在线测评",
        next_action={
            "stage": "interviewing", "step": "一面", "date": None, "time": None, "note": None,
        },
    )
    completed = test_client.post(f"{application_path(application_id)}/complete-next-action", json={
        "expected_revision": 0,
        "outcome": None,
        "summary": None,
        "next_action": None,
    })
    assert completed.status_code == 200, completed.text
    assert (completed.json()["stage"], completed.json()["current_step"]) == (
        "interviewing", "一面",
    )
    assert completed.json()["next_action"] is None


def test_complete_next_action_failure_closes_application_in_one_http_write(client):
    test_client, db_path = client
    application_id = insert_application(
        db_path,
        stage="interviewing",
        current_step="简历筛选",
        next_action={
            "stage": "interviewing", "step": "一面", "date": None,
            "time": None, "note": None,
        },
    )
    completed = test_client.post(f"{application_path(application_id)}/complete-next-action", json={
        "expected_revision": 0,
        "outcome": "failed",
        "summary": "未通过",
        "next_action": {
            "stage": "interviewing", "step": "二面", "date": None,
        },
    })

    assert completed.status_code == 200, completed.text
    detail = completed.json()
    assert (detail["stage"], detail["current_step"], detail["revision"]) == (
        "rejected", "一面", 1,
    )
    assert detail["next_action"] is None
    assert (last_entry(detail)["outcome"], last_entry(detail)["to_stage"]) == (
        "failed", "rejected",
    )


def test_deleting_current_state_entry_rolls_projection_back_safely(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    base = application_path(application_id)
    progressed = test_client.post(f"{base}/progress", json={
        "expected_revision": 0,
        "step": "一面",
        "occurred_date": "2026-07-19",
        "summary": "进入面试",
        "update_current_state": True,
        "target_stage": "interviewing",
        "target_step": "一面",
        "next_action": {
            "stage": "interviewing", "step": "二面", "date": None, "time": None, "note": None,
        },
    })
    assert progressed.status_code == 200, progressed.text
    entry = last_entry(progressed.json())
    deleted = test_client.delete(
        f"{base}/timeline-entries/{entry['id']}",
        params={"expected_revision": 1, "expected_fingerprint": entry["snapshot_fingerprint"]},
    )
    assert deleted.status_code == 200, deleted.text
    detail = deleted.json()
    assert (detail["stage"], detail["current_step"]) == ("backlog", None)
    assert detail["next_action"]["step"] == "二面"
    assert detail["timeline_entries"] == []


def test_timeline_entry_routes_are_application_scoped(client):
    test_client, db_path = client
    application_id = insert_application(db_path)
    created = test_client.post(f"{application_path(application_id)}/progress", json={
        "expected_revision": 0,
        "summary": "真实历程",
        "update_current_state": False,
    }).json()
    entry = last_entry(created)
    assert test_client.put(
        f"/api/timeline/applications/999999/timeline-entries/{entry['id']}",
        json={
            "expected_revision": 1,
            "expected_fingerprint": entry["snapshot_fingerprint"],
            "step": None,
            "occurred_date": None,
            "outcome": None,
            "summary": "不应命中",
        },
    ).status_code == 404
    assert test_client.delete(
        f"{application_path(application_id)}/timeline-entries/999999",
        params={"expected_revision": 1, "expected_fingerprint": "0" * 64},
    ).status_code == 404


def test_review_timeline_edit_returns_not_found_if_entry_disappears_after_lookup(
    client,
    monkeypatch,
):
    test_client, db_path = client
    application_id = insert_application(db_path)

    from careerdesk.features.applications import api as applications_api

    monkeypatch.setattr(
        applications_api.repository,
        "timeline_entry_source",
        lambda *_args, **_kwargs: "review",
    )
    monkeypatch.setattr(
        applications_api.reviews,
        "edit_review_timeline_entry_from_timeline",
        lambda *_args, **_kwargs: None,
    )

    response = test_client.put(
        f"{application_path(application_id)}/timeline-entries/999999",
        json={
            "expected_revision": 0,
            "expected_fingerprint": "0" * 64,
            "step": "不应写入",
            "occurred_date": None,
            "outcome": None,
            "summary": None,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "history entry not found"
    assert scalar(db_path, "SELECT revision FROM applications WHERE id=?", application_id) == 0


def test_agent_explicit_change_updates_stage_step_next_action_and_is_audited(client):
    _test_client, db_path = client
    application_id = insert_application(db_path, stage="applied")
    tool = UpdateApplicationTool(
        db_path,
        "me",
        client_turn_id="d32235e5-2d49-4acd-b31a-a11cce9e39e2",
    )
    result = tool.run({"updates": [{
        "company": "测试公司",
        "new_stage": "interviewing",
        "new_current_step": "一面",
        "next_stage": "interviewing",
        "next_step": "二面",
        "next_date": "2026-07-25",
    }]})
    assert result.status == "success"
    detail = application_repository.application_detail(db_path, "me", application_id)
    assert detail is not None
    assert (detail["stage"], detail["current_step"]) == ("interviewing", "一面")
    assert detail["next_action"]["step"] == "二面"
    assert last_entry(detail)["source"] == "agent"

    replaced = UpdateApplicationTool(
        db_path, "me", client_turn_id="d32235e5-2d49-4acd-b31a-a11cce9e39e3",
    ).run({"updates": [{"company": "测试公司", "replacement_note": "整段替换"}]})
    assert replaced.status == "success"
    cleared = UpdateApplicationTool(
        db_path, "me", client_turn_id="d32235e5-2d49-4acd-b31a-a11cce9e39e4",
    ).run({"updates": [{"company": "测试公司", "clear_note": True}]})
    assert cleared.status == "success"
    assert application_repository.application_detail(
        db_path, "me", application_id,
    )["application_note"] is None


def test_agent_terminal_stage_change_discloses_and_clears_existing_next_action(client):
    _test_client, db_path = client
    application_id = insert_application(
        db_path,
        stage="interviewing",
        current_step="一面",
        next_action={
            "stage": "interviewing",
            "step": "二面",
            "date": "2026-07-25",
            "time": None,
            "note": "准备系统设计",
        },
    )
    result = UpdateApplicationTool(
        db_path,
        "me",
        client_turn_id="9fc55432-3560-4e69-acbd-55e1508c4ecf",
    ).run({"updates": [{
        "company": "测试公司",
        "new_stage": "withdrawn",
    }]})

    assert result.status == "success"
    operation = result.data["results"][0]["operation"]
    assert [change["field"] for change in operation["effect"]["changed_fields"]] == [
        "stage",
        "next_action",
    ]
    detail = application_repository.application_detail(db_path, "me", application_id)
    assert detail is not None
    assert (detail["stage"], detail["current_step"], detail["next_action"]) == (
        "withdrawn",
        "一面",
        None,
    )
    assert last_entry(detail)["source"] == "agent"

    undone = undo_application_update_operation(
        db_path,
        "me",
        operation["operation_id"],
        command_id="89d2285d-3a8d-4fe5-a7aa-6f5cf7cb70c6",
    )
    assert undone["state"] == "undone"
    restored = application_repository.application_detail(db_path, "me", application_id)
    assert restored is not None
    assert (restored["stage"], restored["current_step"], restored["next_action"]) == (
        "interviewing",
        "一面",
        {
            "stage": "interviewing",
            "step": "二面",
            "date": "2026-07-25",
            "time": None,
            "note": "准备系统设计",
        },
    )


def test_concurrent_stage_commands_have_one_winner(client):
    test_client, db_path = client
    application_id = insert_application(db_path, stage="applied")
    endpoint = f"{application_path(application_id)}/stage"
    barrier = Barrier(2)

    def move(stage: str) -> int:
        barrier.wait()
        return test_client.put(endpoint, json={
            "expected_revision": 0, "stage": stage, "origin": "board_drag",
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        codes = list(executor.map(move, ("written_test", "interviewing")))
    assert sorted(codes) == [200, 409]
    assert scalar(db_path, "SELECT revision FROM applications WHERE id=?", application_id) == 1
    assert scalar(
        db_path, "SELECT COUNT(*) FROM timeline_entries WHERE application_id=?", application_id,
    ) == 1


def test_next_action_validation_rejects_unanchored_time_and_terminal_plan(client):
    test_client, _db_path = client
    invalid_time = test_client.post("/api/timeline/applications", json={
        "company": "时间公司",
        "position": "时间岗位",
        "stage": "applied",
        "next_action": {
            "stage": "interviewing", "step": "一面", "date": None,
            "time": "10:00", "note": None,
        },
    })
    assert invalid_time.status_code == 422
    terminal = test_client.post("/api/timeline/applications", json={
        "company": "结束公司",
        "position": "结束岗位",
        "stage": "rejected",
        "next_action": {
            "stage": "interviewing", "step": "复活", "date": None,
            "time": None, "note": None,
        },
    })
    assert terminal.status_code == 422
