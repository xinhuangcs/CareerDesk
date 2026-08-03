
import json
from pathlib import Path

import pytest
from tests.support import ScriptedLLM
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.resumes import repository as resumes
from careerdesk.features.resumes.service import ResumeService


RESUME_PARSE = {
    "family": "backend",
    "lines": [{"line_index": 0, "knowledge_points": ["幂等"]}],
}


def _db(tmp_path) -> str:
    path = str(tmp_path / "data" / "careerdesk.db")
    init_db(path)
    return path


def _seed_application(db_path: str, user_id: str, company: str = "C", position: str = "P") -> int:
    with transaction(db_path) as conn:
        return conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, company, position, now_iso(), now_iso()),
        ).lastrowid


def test_cross_tenant_application_and_resume_ids_are_rejected_before_llm(tmp_path):
    db_path = _db(tmp_path)
    victim_application = _seed_application(db_path, "victim")
    victim_resume = resumes.upsert_resume(db_path, "victim", "victim-r", "secret", lines=[])
    assert victim_resume is not None

    resume_result = __import__("asyncio").run(
        ResumeService(db_path, ScriptedLLM([])).register(
            "attacker", "x", "content", binding="application",
            application_id=victim_application,
        )
    )
    assert resume_result["status"] == "error" and "无权访问" in resume_result["message"]
    with pytest.raises(ValueError, match="无权访问"):
        resumes.upsert_resume(
            db_path, "attacker", "x", "content", binding="application",
            application_id=victim_application, lines=[],
        )

    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resumes WHERE user_id='attacker'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM grill_sessions WHERE user_id='attacker'").fetchone()[0] == 0


def test_new_binding_and_archive_invalidate_only_resume_dependent_prep(tmp_path):
    import asyncio

    db_path = _db(tmp_path)
    application_id = _seed_application(db_path, "u")
    llm = ScriptedLLM([
        json.dumps(RESUME_PARSE, ensure_ascii=False),
        json.dumps(RESUME_PARSE, ensure_ascii=False),
    ])
    service = ResumeService(db_path, llm)
    first = asyncio.run(service.register(
        "u", "A", "old", binding="application", application_id=application_id,
    ))
    assert first["status"] == "ok"

    stale = {"research": "cached",
             "resume_adaptation": {"report": "old"}, "nontech_answers": ["old"]}
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_status='ready', prep_json=? WHERE id=?",
            (json.dumps(stale, ensure_ascii=False), application_id),
        )
    second = asyncio.run(service.register(
        "u", "B", "new", binding="application", application_id=application_id,
    ))
    assert second["status"] == "ok"
    with read_connection(db_path) as conn:
        bound, status, prep_json = conn.execute(
            "SELECT resume_id, prep_status, prep_json FROM applications WHERE id=?",
            (application_id,),
        ).fetchone()
    assert bound == second["resume_id"] and status == "ready"
    assert json.loads(prep_json) == {"research": "cached", "nontech_answers": ["old"]}

    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_status='ready', prep_json=? WHERE id=?",
            (json.dumps(stale, ensure_ascii=False), application_id),
        )
    assert resumes.archive_resume(db_path, "u", second["resume_id"])
    with read_connection(db_path) as conn:
        status, prep_json = conn.execute(
            "SELECT prep_status, prep_json FROM applications WHERE id=?", (application_id,)
        ).fetchone()
    assert status == "ready" and json.loads(prep_json) == {
        "research": "cached", "nontech_answers": ["old"],
    }


def test_grill_experiment_intro_is_durable_once_per_release(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    from careerdesk.features.grill import repository as grill_repository
    release = {"version": "1.0.0"}
    monkeypatch.setattr(
        grill_repository, "_application_version", lambda: release["version"],
    )
    with TestClient(create_app()) as client:
        first = client.post("/api/grill/experiment-intro/claim", json={})
        second = client.post("/api/grill/experiment-intro/claim", json={})
        assert first.json() == {"should_show": True, "release_version": "1.0.0"}
        assert second.json() == {"should_show": False, "release_version": "1.0.0"}

        release["version"] = "1.1.0"
        updated = client.post("/api/grill/experiment-intro/claim", json={})
        updated_again = client.post("/api/grill/experiment-intro/claim", json={})
        assert updated.json() == {"should_show": True, "release_version": "1.1.0"}
        assert updated_again.json() == {"should_show": False, "release_version": "1.1.0"}

        release["version"] = "1.0.0"
        original_again = client.post("/api/grill/experiment-intro/claim", json={})
        assert original_again.json() == {
            "should_show": False, "release_version": "1.0.0",
        }

    get_settings.cache_clear()

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "legacy-data"))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        migrated = client.post(
            "/api/grill/experiment-intro/claim", json={"previously_seen": True},
        )
        assert migrated.json() == {
            "should_show": True, "release_version": "1.0.0",
        }
    get_settings.cache_clear()


def test_api_rejects_bad_grill_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        assert client.post("/api/grill/start", json={"mode": "typo"}).status_code == 422
        assert client.post("/api/grill/start", json={"question_count": 0}).status_code == 422
        assert client.post("/api/grill/start", json={"question_count": 21}).status_code == 422
        assert client.post("/api/grill/answer", json={"session_id": 1, "text": ""}).status_code == 422
        assert client.post("/api/grill/answer", json={"session_id": 1, "text": "有效回答"}).status_code == 422
        assert client.post(
            "/api/grill/answer", json={"session_id": 1, "question_id": 1, "text": "   "}
        ).status_code == 422
        assert client.post("/api/grill/skip", json={"session_id": 1}).status_code == 422

    get_settings.cache_clear()


def test_runtime_image_uses_the_supported_entrypoint():
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")
    assert "apt-get install -y --no-install-recommends ca-certificates" in dockerfile
    assert "apt-get install -y --no-install-recommends git" not in dockerfile
    assert "COPY frontend/package.json frontend/package-lock.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "uv sync --project backend --locked" in dockerfile
    assert 'APP_RUNTIME_MODE="server"' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert (
        'CMD ["python", "-m", "uvicorn", "careerdesk.bootstrap.app:app", '
        '"--workers", "1", "--host", "0.0.0.0", "--port", "8000"]'
    ) in dockerfile
    assert "/srv/www" not in dockerfile
    assert 'CMD ["sh", "-c"' not in dockerfile
