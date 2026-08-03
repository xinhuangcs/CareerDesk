
import asyncio
import json
from pathlib import Path

import pytest
from tests.support import ScriptedLLM

from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.resumes.repository import (
    archive_resume,
    get_active_resume_text,
    pick_resume_for_application,
    update_active_resume_text,
    upsert_resume,
)
from careerdesk.features.resumes import jobs as resume_jobs
from careerdesk.features.resumes.service import ResumeService
from careerdesk.platform.storage.uploads import user_upload_root

TODAY = "2026-07-07"

RESUME_TEXT = "自研 agent 框架 CareerDesk：journal 可重放；实现检查点幂等恢复。\n熟悉 Docker 部署。"

RESUME_PARSE = {
    "family": "agent_app",
    "lines": [
        {"line_index": 0,
         "knowledge_points": ["检查点幂等", "重放幂等"]},
        {"line_index": 1, "knowledge_points": ["Docker"]},
    ],
}

PROBES = {
    "entries": [
        {"line_index": 0,
         "probes": ["恢复时怎么去重？", "checkpoint 存了什么状态？"]},
        {"line_index": 1,
         "probes": ["镜像怎么做多阶段构建？", "如何控制镜像体积？"]},
    ]
}

RESEARCH = {"business": "b", "culture": "c", "recent_news": "n", "interview_style": "s",
            "likely_questions": ["为什么来我们公司？"]}


def scripted(*payloads) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(payload, ensure_ascii=False) for payload in payloads])


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "biz.db")
    init_db(path)
    return path


def seed_application(db_path: str) -> int:
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO applications (user_id, company, position, jd_text, created_time, updated_time) "
            "VALUES ('u1', '字节', 'LLM应用', 'JD：熟悉 Agent 工程与 RAG', ?, ?)",
            (now_iso(), now_iso()),
        )
        return cursor.lastrowid


def test_library_list_labels_and_explicit_text_view_are_tenant_safe(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from careerdesk.bootstrap.app import create_app
    from careerdesk.core.config import get_settings

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    user_id = settings.dev_fake_user
    assert user_id is not None
    with transaction(settings.db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO applications (user_id, company, position, jd_text, created_time, updated_time) "
            "VALUES (?, '示例公司', '平台工程师', 'JD', ?, ?)",
            (user_id, now_iso(), now_iso()),
        )
        application_id = cursor.lastrowid
    general_id = upsert_resume(
        settings.db_path,
        user_id,
        "通用简历",
        "通用版提取文字",
        family="backend",
    )
    specific_id = upsert_resume(
        settings.db_path,
        user_id,
        "岗位简历",
        "岗位版提取文字",
        family="algorithm",
        binding="application",
        application_id=application_id,
    )
    foreign_id = upsert_resume(settings.db_path, "other-user", "他人简历", "不可见正文")
    assert general_id and specific_id and foreign_id

    with TestClient(create_app()) as client:
        items = client.get("/api/resumes").json()["items"]
        by_id = {item["id"]: item for item in items}
        assert "family" not in by_id[general_id]
        assert "content_text" not in by_id[general_id]
        assert by_id[general_id]["application_company"] is None
        assert by_id[general_id]["application_position"] is None
        assert by_id[specific_id]["application_company"] == "示例公司"
        assert by_id[specific_id]["application_position"] == "平台工程师"

        text_response = client.get(f"/api/resumes/{general_id}/text")
        assert text_response.status_code == 200
        assert text_response.json()["content_text"] == "通用版提取文字"
        original_hash = text_response.json()["content_hash"]
        update_response = client.put(
            f"/api/resumes/{general_id}/text",
            json={"content_text": "人工核对后的校正版文字", "expected_content_hash": original_hash},
        )
        assert update_response.status_code == 200
        assert update_response.json()["content_text"] == "人工核对后的校正版文字"
        assert update_response.json()["content_hash"] != original_hash
        assert client.put(
            f"/api/resumes/{general_id}/text",
            json={"content_text": "过期窗口的修改", "expected_content_hash": original_hash},
        ).status_code == 409
        with read_connection(settings.db_path) as conn:
            row = conn.execute(
                "SELECT lines_json, annotation_status FROM resumes WHERE id = ?",
                (general_id,),
            ).fetchone()
        assert row == (None, "pending")
        assert client.get(f"/api/resumes/{foreign_id}/text").status_code == 404
        assert client.put(
            f"/api/resumes/{foreign_id}/text",
            json={"content_text": "越权修改", "expected_content_hash": "0" * 64},
        ).status_code == 404
        assert archive_resume(settings.db_path, user_id, general_id)
        assert client.get(f"/api/resumes/{general_id}/text").status_code == 404
        assert client.put(
            f"/api/resumes/{general_id}/text",
            json={
                "content_text": "归档后修改",
                "expected_content_hash": update_response.json()["content_hash"],
            },
        ).status_code == 404

    get_settings.cache_clear()


def test_register_parses_and_binds_application(db_path):
    application_id = seed_application(db_path)
    service = ResumeService(db_path, scripted(RESUME_PARSE))

    result = run(service.register("u1", "字节Seed专属", RESUME_TEXT,
                                  binding="application", application_id=application_id))
    assert result["status"] == "ok" and result["family"] == "agent_app" and result["line_count"] == 2
    with read_connection(db_path) as conn:
        lines_json, = conn.execute("SELECT lines_json FROM resumes WHERE name='字节Seed专属'").fetchone()
        bound_resume_id, = conn.execute("SELECT resume_id FROM applications WHERE id=?",
                                        (application_id,)).fetchone()
    assert json.loads(lines_json)[0]["knowledge_points"] == ["检查点幂等", "重放幂等"]
    assert bound_resume_id == result["resume_id"]


def test_manual_text_correction_invalidates_only_resume_dependent_prep(db_path):
    application_id = seed_application(db_path)
    resume_id = upsert_resume(
        db_path,
        "u1",
        "人工校正版",
        "原识别文字",
        binding="application",
        application_id=application_id,
        lines=[{"text": "原识别文字", "knowledge_points": ["旧标注"]}],
    )
    assert resume_id
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            (json.dumps({"resume_adaptation": {"old": True}, "company_research": {"keep": True}}),
             application_id),
        )

    before = get_active_resume_text(db_path, "u1", resume_id)
    assert before is not None
    status, corrected = update_active_resume_text(
        db_path,
        "u1",
        resume_id,
        "人工核对后的文字",
        expected_content_hash=before["content_hash"],
    )

    assert status == "ok"
    assert corrected is not None and corrected["content_text"] == "人工核对后的文字"
    with read_connection(db_path) as conn:
        prep_json, lines_json, annotation_status = conn.execute(
            "SELECT applications.prep_json, resumes.lines_json, resumes.annotation_status "
            "FROM applications JOIN resumes ON resumes.id = applications.resume_id "
            "WHERE applications.id = ?",
            (application_id,),
        ).fetchone()
    assert json.loads(prep_json) == {"company_research": {"keep": True}}
    assert lines_json is None
    assert annotation_status == "pending"


def test_resume_job_state_survives_independent_reads(db_path, tmp_path):
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"pdf")

    job = resume_jobs.start_job(
        db_path,
        "u1",
        operation="create",
        name="后台简历",
        file_path=str(source),
    )
    assert resume_jobs.list_jobs(db_path, "u1")[0]["stage"] == "queued"

    assert resume_jobs.update_job(db_path, "u1", job["job_id"], stage="parsing")
    assert resume_jobs.list_jobs(db_path, "u1")[0]["state"] == "processing"

    assert resume_jobs.update_job(
        db_path,
        "u1",
        job["job_id"],
        state="completed",
        stage="completed",
        message="简历已解析并保存。",
        resume_id=7,
    )
    completed = resume_jobs.list_jobs(db_path, "u1")[0]
    assert completed["state"] == "completed" and completed["resume_id"] == 7
    assert "file_path" not in completed
    assert resume_jobs.list_jobs(db_path, "u2") == []


def test_resume_terminal_job_dismiss_is_tenant_scoped_idempotent_and_rejects_active(
    db_path,
):
    job = resume_jobs.start_job(
        db_path,
        "u1",
        operation="create",
        name="可关闭任务",
        file_path="/tmp/resume.md",
    )

    with pytest.raises(resume_jobs.ResumeJobConflict, match="仍在处理中"):
        resume_jobs.dismiss_job(db_path, "u1", job["job_id"])

    assert resume_jobs.dismiss_job(db_path, "u2", job["job_id"]) is False
    assert resume_jobs.list_jobs(db_path, "u1")[0]["state"] == "processing"
    assert resume_jobs.update_job(
        db_path,
        "u1",
        job["job_id"],
        state="failed",
        stage="failed",
        message="解析失败",
    )

    assert resume_jobs.dismiss_job(db_path, "u2", job["job_id"]) is False
    assert resume_jobs.dismiss_job(db_path, "u1", job["job_id"]) is True
    assert resume_jobs.dismiss_job(db_path, "u1", job["job_id"]) is False
    assert resume_jobs.list_jobs(db_path, "u1") == []


def test_resume_terminal_job_dismiss_api_rejects_processing_and_replays_absent(
    tmp_path,
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from careerdesk.bootstrap.app import create_app
    from careerdesk.core.config import get_settings

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    user_id = settings.dev_fake_user
    assert user_id is not None
    job = resume_jobs.start_job(
        settings.db_path,
        user_id,
        operation="create",
        name="API 关闭任务",
        file_path="/tmp/resume.md",
    )

    with TestClient(create_app()) as client:
        processing = client.delete(f"/api/resumes/jobs/{job['job_id']}")
        assert processing.status_code == 409
        assert "仍在处理中" in processing.json()["detail"]

        assert resume_jobs.update_job(
            settings.db_path,
            user_id,
            job["job_id"],
            state="failed",
            stage="failed",
            message="解析失败",
        )
        dismissed = client.delete(f"/api/resumes/jobs/{job['job_id']}")
        replayed = client.delete(f"/api/resumes/jobs/{job['job_id']}")
        malformed = client.delete("/api/resumes/jobs/not-a-uuid")

    assert dismissed.status_code == 200
    assert dismissed.json() == {"status": "ok", "dismissed": True}
    assert replayed.json() == {"status": "ok", "dismissed": False}
    assert malformed.status_code == 422
    get_settings.cache_clear()


def test_resume_job_rejects_invalid_operation_and_state_combinations(db_path):
    with pytest.raises(ValueError, match="target_resume_id"):
        resume_jobs.start_job(
            db_path, "u1", operation="create", name="v1",
            file_path="/tmp/v1.md", target_resume_id=1,
        )
    with pytest.raises(ValueError, match="target_resume_id"):
        resume_jobs.start_job(
            db_path, "u1", operation="update", name="v1", file_path="/tmp/v1.md",
        )
    job = resume_jobs.start_job(
        db_path, "u1", operation="create", name="v1", file_path="/tmp/v1.md",
    )
    with pytest.raises(ValueError, match="状态无效"):
        resume_jobs.update_job(
            db_path, "u1", job["job_id"], state="completed", stage="parsing",
            resume_id=1,
        )
    with pytest.raises(ValueError, match="状态无效"):
        resume_jobs.update_job(
            db_path, "u1", job["job_id"], state="completed", stage="completed",
        )


def test_register_file_replacement_removes_previous_managed_copy(db_path, tmp_path):
    uploads = user_upload_root(Path(db_path).parent, "resumes", "u1")
    uploads.mkdir(parents=True, exist_ok=True)
    old_file = uploads / "old.pdf"
    new_file = uploads / "new.pdf"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    resume_id = upsert_resume(
        db_path, "u1", "主简历", RESUME_TEXT,
        lines=[{
            "text": RESUME_TEXT.splitlines()[0],
            "knowledge_points": ["检查点幂等", "重放幂等"],
        }],
        file_path=str(old_file),
    )
    assert resume_id is not None

    result = run(ResumeService(db_path, scripted(RESUME_PARSE)).register(
        "u1", "主简历", RESUME_TEXT, file_path=str(new_file), replace_existing=True,
    ))

    assert result["status"] == "ok"
    assert not old_file.exists() and new_file.exists()


def test_archived_bound_resume_falls_back_to_latest_active(db_path):
    application_id = seed_application(db_path)
    fallback_id = upsert_resume(
        db_path, "u1", "通用兜底", "active", family="backend",
        lines=[{"text": "仍可用", "knowledge_points": []}],
    )
    bound_id = upsert_resume(
        db_path, "u1", "岗位专属", "archived", family="backend", binding="application",
        application_id=application_id,
        lines=[{"text": "已归档", "knowledge_points": []}],
    )
    assert bound_id is not None and archive_resume(db_path, "u1", bound_id)

    picked = pick_resume_for_application(db_path, "u1", application_id)
    assert picked is not None and picked["id"] == fallback_id and picked["archived"] is False

    assert fallback_id is not None and archive_resume(db_path, "u1", fallback_id)
    assert pick_resume_for_application(db_path, "u1", application_id) is None
