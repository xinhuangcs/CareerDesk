"""Natural-key, applied-date, pause metadata, and local-date regressions."""

from datetime import datetime
from sqlite3 import IntegrityError
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.features.applications import repository
from careerdesk.features.applications.contracts import BoardItemDTO
from careerdesk.features.applications.operations import (
    ApplicationUpdateOperationConflict,
    execute_application_update_operation,
    undo_application_update_operation,
)
from careerdesk.features.companies.public import ensure_company_in_transaction
from careerdesk.platform.database import init_db, read_connection, transaction


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "identity.db")
    init_db(path)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_TIMEZONE", "Europe/Copenhagen")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client, str(tmp_path / "careerdesk.db")
    get_settings.cache_clear()


def _create(db_path: str, *, stage: str = "backlog") -> int:
    return repository.create_application_profile(
        db_path,
        "u1",
        company="示例 公司",
        position="AI 工程师",
        department=None,
        channel=None,
        stage=stage,
        current_step=None,
        next_action=None,
        jd_text=None,
        timezone_name="Europe/Copenhagen",
    )


def test_generated_identity_keys_unify_company_and_application_writes(db_path):
    application_id = _create(db_path)
    with transaction(db_path) as conn:
        first_company_id = ensure_company_in_transaction(conn, "u1", "示例 公司")
        second_company_id = ensure_company_in_transaction(conn, "u1", "示例公司")
        assert first_company_id == second_company_id
        with pytest.raises(IntegrityError):
            conn.execute(
                "INSERT INTO applications "
                "(user_id, company, position, created_time, updated_time) "
                "VALUES ('u1', '示例公司', 'AI工程师', 'created', 'updated')",
            )

    assert repository.resolve_application_by_name(
        db_path, "u1", "示例公司", "AI工程师",
    )["id"] == application_id
    assert repository.resolve_application_by_name(
        db_path, "u1", "示 例 公 司", "A I 工 程 师",
    )["id"] == application_id
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT company_key, position_key FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("示例公司", "AI工程师")
        assert conn.execute(
            "SELECT COUNT(*) FROM companies WHERE user_id = 'u1'",
        ).fetchone() == (1,)


def test_fuzzy_company_reads_share_the_persisted_whitespace_identity(db_path):
    application_id = _create(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', '百分比%公司', '数据工程师', 'created', 'updated')",
        )

    assert [item["id"] for item in repository.find_applications_by_company(
        db_path, "u1", "例公", fuzzy=True,
    )] == [application_id]
    assert [item["id"] for item in repository.find_applications_by_company(
        db_path, "u1", "示 例 公 司", fuzzy=True,
    )] == [application_id]
    assert [item["id"] for item in repository.find_applications_by_company(
        db_path, "u1", "例公", fuzzy=True, position="AI工程师",
    )] == [application_id]
    assert repository.find_applications_by_company(
        db_path, "u1", "\u2003\u3000", fuzzy=True,
    ) == []
    assert repository.find_applications_by_company(  # type: ignore[arg-type]
        db_path, "u1", 42, fuzzy=True,
    ) == []
    assert [item["company"] for item in repository.find_applications_by_company(
        db_path, "u1", "%", fuzzy=True,
    )] == ["百分比%公司"]


def test_create_api_rejects_whitespace_equivalent_duplicate(client):
    test_client, db_path = client
    payload = {
        "company": "示例 公司",
        "position": "AI 工程师",
        "department": None,
        "channel": None,
        "stage": "backlog",
        "current_step": None,
        "next_action": None,
        "jd_text": None,
    }
    assert test_client.post("/api/timeline/applications", json=payload).status_code == 201
    duplicate = test_client.post("/api/timeline/applications", json={
        **payload,
        "company": "示例公司",
        "position": "AI工程师",
    })
    assert duplicate.status_code == 409
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone() == (1,)


def test_profile_can_correct_applied_date_and_pooled_reason(client):
    test_client, _ = client
    created = test_client.post("/api/timeline/applications", json={
        "company": "日期公司",
        "position": "平台工程师",
        "department": None,
        "channel": "官网",
        "stage": "applied",
        "current_step": "已提交",
        "applied_date": "2026-02-03",
        "pause_reason": None,
        "next_action": None,
        "jd_text": None,
    })
    assert created.status_code == 201
    detail = created.json()
    assert detail["applied_date"] == "2026-02-03"
    assert detail["timeline_entries"][-1]["occurred_date"] == "2026-02-03"

    pooled = test_client.put(
        f"/api/timeline/applications/{detail['id']}/profile",
        json={
            "expected_revision": detail["revision"],
            "company": detail["company"],
            "position": detail["position"],
            "department": detail["department"],
            "channel": detail["channel"],
            "stage": "pooled",
            "current_step": detail["current_step"],
            "applied_date": "2026-02-04",
            "pause_reason": "HC 暂停，三月再联系",
            "next_action": None,
            "jd_text": detail["jd_text"],
        },
    )
    assert pooled.status_code == 200
    assert pooled.json()["applied_date"] == "2026-02-04"
    assert pooled.json()["pause_reason"] == "HC 暂停，三月再联系"

    invalid = test_client.put(
        f"/api/timeline/applications/{detail['id']}/profile",
        json={
            "expected_revision": pooled.json()["revision"],
            "company": detail["company"],
            "position": detail["position"],
            "department": detail["department"],
            "channel": detail["channel"],
            "stage": "applied",
            "current_step": detail["current_step"],
            "applied_date": "2026-02-04",
            "pause_reason": "不应保留",
            "next_action": None,
            "jd_text": detail["jd_text"],
        },
    )
    assert invalid.status_code == 422


def test_pause_metadata_is_rejected_outside_pool_at_db_and_response_boundaries(db_path):
    application_id = _create(db_path)
    with transaction(db_path) as conn, pytest.raises(IntegrityError):
        conn.execute(
            "UPDATE applications SET pause_reason = '不应存在' WHERE id = ?",
            (application_id,),
        )
    with pytest.raises(ValueError, match="非泡池子"):
        BoardItemDTO.model_validate({
            "id": application_id,
            "company": "示例公司",
            "position": "AI工程师",
            "department": None,
            "channel": None,
            "stage": "backlog",
            "current_step": None,
            "next_action": None,
            "paused_from_stage": None,
            "pause_reason": "不应存在",
            "priority": None,
            "created_time": "2026-01-01T00:00:00+00:00",
            "applied_date": None,
            "prep_status": "none",
            "revision": 0,
            "last_activity_time": None,
        })


def test_agent_state_write_binds_trusted_occurred_date_and_undo_restores_date(db_path):
    application_id = _create(db_path)
    operation_id = str(uuid4())
    turn_id = str(uuid4())
    kwargs = {
        "operation_id": operation_id,
        "client_turn_id": turn_id,
        "company": "示例公司",
        "position": "AI工程师",
        "changes": {"stage": "applied"},
        "occurred_date": "2026-01-02",
    }
    completed = execute_application_update_operation(db_path, "u1", **kwargs)
    assert completed["before"]["applied_date"] is None
    assert completed["final"]["applied_date"] == "2026-01-02"
    assert execute_application_update_operation(db_path, "u1", **kwargs) == completed
    with pytest.raises(ApplicationUpdateOperationConflict, match="另一条"):
        execute_application_update_operation(
            db_path,
            "u1",
            **{**kwargs, "occurred_date": "2026-01-03"},
        )
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT stage, applied_date FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("applied", "2026-01-02")
        assert conn.execute(
            "SELECT occurred_date FROM timeline_entries WHERE application_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (application_id,),
        ).fetchone() == ("2026-01-02",)

    undone = undo_application_update_operation(
        db_path,
        "u1",
        operation_id,
        command_id=uuid4(),
    )
    assert undone["state"] == "undone"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT stage, applied_date FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("backlog", None)


def test_agent_display_only_company_rename_reuses_identity_and_can_undo(db_path):
    application_id = _create(db_path)
    with read_connection(db_path) as conn:
        company_id = conn.execute(
            "SELECT company_id FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0]

    completed = execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company="示例公司",
        position="AI工程师",
        changes={"company": "示例公司"},
        occurred_date="2026-01-02",
    )

    assert completed["state"] == "completed"
    assert completed["before"]["company_id"] == company_id
    assert completed["final"]["company_id"] == company_id
    assert completed["effect"]["company_record_created"] is False
    assert completed["effect"]["changed_fields"] == [{
        "field": "company", "before": "示例 公司", "after": "示例公司",
    }]
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT company, company_id FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("示例公司", company_id)
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone() == (1,)

    undone = undo_application_update_operation(
        db_path,
        "u1",
        completed["operation_id"],
        command_id=uuid4(),
    )
    assert undone["state"] == "undone"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT company, company_id FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("示例 公司", company_id)


def test_first_manual_transition_to_applied_uses_configured_local_date(db_path):
    application_id = _create(db_path)
    timezone_name = "Pacific/Kiritimati"
    expected = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    detail = repository.record_application_progress(
        db_path,
        "u1",
        application_id,
        expected_revision=0,
        step="提交申请",
        occurred_date=None,
        outcome=None,
        summary="已投递",
        update_current_state=True,
        target_stage="applied",
        target_step="已提交",
        replace_next_action=False,
        next_action=None,
        timezone_name=timezone_name,
    )
    assert detail is not None
    assert detail["applied_date"] == expected
    assert detail["timeline_entries"][-1]["occurred_date"] == expected
