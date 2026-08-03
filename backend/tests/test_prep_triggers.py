
import json

from tests.support import ScriptedLLM
from fastapi.testclient import TestClient
import pytest

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.applications import repository as timeline

RESUME_PARSE = {"family": "backend",
                "lines": [{"line_index": 0, "knowledge_points": ["RAG"]}]}


@pytest.fixture(autouse=True)
def _hermetic_deepseek_credential(monkeypatch):
    """Client construction is real but never relies on a developer's .env."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "hermetic-test-key")
    monkeypatch.setenv("APP_LLM_CONTEXT_WINDOW", "1000000")
    monkeypatch.setenv("APP_LLM_MAX_OUTPUT_TOKENS", "393216")


def scripted(*payloads) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(payload, ensure_ascii=False) for payload in payloads])


class _StubPrepService:

    def __init__(self, calls: list):
        self._calls = calls

    async def run(self, user_id: str, application_id: int, **kwargs) -> dict:
        self._calls.append((user_id, application_id, kwargs))
        return {"status": "ok"}


def seed_application(db_path: str, user_id: str, prep_status: str = "none",
                     position: str = "后端开发") -> int:
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO applications (user_id, company, position, stage, prep_status, "
            "created_time, updated_time) VALUES (?, '腾讯', ?, 'applied', ?, ?, ?)",
            (user_id, position, prep_status, now_iso(), now_iso()))
        return cursor.lastrowid


def read_state(db_path: str, application_id: int) -> tuple:
    with read_connection(db_path) as conn:
        return conn.execute("SELECT prep_status, priority FROM applications WHERE id = ?",
                            (application_id,)).fetchone()


def test_briefing_endpoint_returns_base_page_before_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("APP_LLM_CONTEXT_WINDOW")
    monkeypatch.delenv("APP_LLM_MAX_OUTPUT_TOKENS")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        response = client.get(f"/api/timeline/applications/{application_id}/briefing")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "公司调研还没生成" in body["markdown"]
    assert set(body["data"]) == {
        "application", "research", "research_stale", "position_report", "anchor", "sources",
    }
    get_settings.cache_clear()


def test_prep_endpoint_marks_pending_and_kicks(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        body = client.post(f"/api/timeline/applications/{application_id}/prep").json()
    assert body["status"] == "started"
    assert body["refresh_applied"] is False and body["takeover_applied"] is False
    assert read_state(settings.db_path, application_id) == ("pending", None)
    assert len(calls) == 1
    assert calls[0][:2] == (settings.dev_fake_user, application_id)
    assert calls[0][2]["refresh_research"] is False
    get_settings.cache_clear()


def test_prep_endpoint_localizes_system_messages_for_english(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.delenv("APP_LLM_CONTEXT_WINDOW")
    monkeypatch.delenv("APP_LLM_MAX_OUTPUT_TOKENS")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep?output_locale=en",
        ).json()

    assert body == {
        "status": "error",
        "message": "Company research requires a model. Configure one under Model & Privacy first.",
    }
    get_settings.cache_clear()


def test_prep_endpoint_forwards_refresh_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "true")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep?refresh_research=true"
        ).json()

    assert body["status"] == "started" and body["refresh_applied"] is True
    assert body["takeover_applied"] is False and body["reused"] is False
    assert len(calls) == 1 and calls[0][2]["refresh_research"] is True
    get_settings.cache_clear()


def test_ready_endpoint_reuses_stale_page_open_but_explicit_refresh_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "true")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(
        settings.db_path,
        settings.dev_fake_user,
        prep_status="ready",
    )
    calls: list = []
    monkeypatch.setattr(
        "careerdesk.orchestration.application_prep.factory.build_prep_service",
        lambda _settings, **_kwargs: _StubPrepService(calls),
    )

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        reused = client.post(f"/api/timeline/applications/{application_id}/prep").json()
        restarted = client.post(
            f"/api/timeline/applications/{application_id}/prep?refresh_research=true",
        ).json()

    assert reused["status"] == "completed" and reused["prep_status"] == "ready"
    assert restarted["status"] == "started" and restarted["refresh_applied"] is True
    assert len(calls) == 1 and calls[0][2]["refresh_research"] is True
    get_settings.cache_clear()


def test_adaptation_recovery_action_restarts_ready_prep_instead_of_reusing_it(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "true")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(
        settings.db_path,
        settings.dev_fake_user,
        prep_status="ready",
    )
    with transaction(settings.db_path) as conn:
        resume_id = conn.execute(
            "INSERT INTO resumes "
            "(user_id, name, content_text, content_hash, extraction_receipt_json, segments_json, "
            "binding, archived, created_time, updated_time) VALUES "
            "(?, '恢复测试版', '后端工程师\\n负责服务交付。', ?, '{}', '[]', "
            "'family', 0, ?, ?)",
            (settings.dev_fake_user, "3" * 64, now_iso(), now_iso()),
        ).lastrowid
        conn.execute(
            "UPDATE applications SET department='研发', jd_text='负责后端服务交付。', "
            "resume_id=?, prep_json=? WHERE id=?",
            (
                resume_id,
                json.dumps({
                    "research_attempt": {
                        "attempt_state": "succeeded",
                        "generation": "completed-leg",
                        "updated_time": now_iso(),
                        "error_code": None,
                    },
                    "research_snapshot": {"snapshot_version": "corrupt"},
                }),
                application_id,
            ),
        )
    calls: list = []
    monkeypatch.setattr(
        "careerdesk.orchestration.application_prep.factory.build_prep_service",
        lambda _settings, **_kwargs: _StubPrepService(calls),
    )

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        adaptation = client.get(
            f"/api/timeline/applications/{application_id}/resume-adaptation",
        ).json()
        assert adaptation["state"] == "research_required"
        assert adaptation["research"]["action"] == "restart"
        restarted = client.post(
            f"/api/timeline/applications/{application_id}/prep?refresh_research=true",
        ).json()

    assert restarted["status"] == "started" and restarted["refresh_applied"] is True
    assert len(calls) == 1 and calls[0][2]["refresh_research"] is True
    get_settings.cache_clear()


def test_disabled_web_research_still_restarts_ready_non_web_briefing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "false")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(
        settings.db_path,
        settings.dev_fake_user,
        prep_status="ready",
    )
    calls: list = []
    monkeypatch.setattr(
        "careerdesk.orchestration.application_prep.factory.build_prep_service",
        lambda _settings, **_kwargs: _StubPrepService(calls),
    )

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep?refresh_research=true",
        ).json()

    assert body["status"] == "started"
    assert body["refresh_requested"] is True
    assert body["refresh_applied"] is False
    assert "当前未启用" in body["message"] and "不会刷新网页材料" in body["message"]
    assert "岗位报告、建议答案" in body["message"] and "已开始生成" in body["message"]
    assert len(calls) == 1 and calls[0][2]["refresh_research"] is False
    assert read_state(settings.db_path, application_id)[0] == "pending"
    get_settings.cache_clear()


def test_disabled_web_research_started_response_explains_downgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "false")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)
    calls: list = []
    monkeypatch.setattr(
        "careerdesk.orchestration.application_prep.factory.build_prep_service",
        lambda _settings, **_kwargs: _StubPrepService(calls),
    )

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep?refresh_research=true",
        ).json()

    assert body["status"] == "started"
    assert body["refresh_requested"] is True and body["refresh_applied"] is False
    assert "当前未启用" in body["message"] and "岗位报告、建议答案" in body["message"]
    assert len(calls) == 1 and calls[0][2]["refresh_research"] is False
    get_settings.cache_clear()


def test_strict_cloud_prep_rejected_before_durable_generation_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "openai:gpt-4o-mini")
    monkeypatch.setenv("APP_STRICT_OFFLINE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "configured-but-dormant")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        response = client.post(f"/api/timeline/applications/{application_id}/prep")

    assert response.status_code == 409
    assert response.json()["code"] == "strict_offline"
    with read_connection(settings.db_path) as conn:
        state = conn.execute(
            "SELECT prep_status, prep_generation, prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
    assert state == ("none", None, None)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("model", "missing_key"),
    [
        ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY"),
        ("not-a-provider:model", None),
    ],
)
def test_prep_model_build_failure_precedes_durable_claim(
    tmp_path,
    monkeypatch,
    model,
    missing_key,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", model)
    if missing_key is not None:
        monkeypatch.delenv(missing_key, raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        body = client.post(f"/api/timeline/applications/{application_id}/prep").json()

    assert body == {
        "status": "error",
        "message": "调研模型初始化失败，请检查「模型与隐私」后重试",
    }
    with read_connection(settings.db_path) as connection:
        state = connection.execute(
            "SELECT prep_status, prep_generation, prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
    assert state == ("none", None, None)
    get_settings.cache_clear()


def test_prep_builds_llm_once_and_injects_same_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)
    sentinel = object()
    builds: list[tuple] = []
    injected: list[object] = []
    calls: list = []

    def build_once(*args, **kwargs):
        builds.append((args, kwargs))
        return sentinel

    def service_for(_settings, *, llm):
        injected.append(llm)
        return _StubPrepService(calls)

    monkeypatch.setattr(
        "careerdesk.orchestration.application_prep.factory.build_llm",
        build_once,
    )
    monkeypatch.setattr(
        "careerdesk.orchestration.application_prep.factory.build_prep_service",
        service_for,
    )

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        body = client.post(f"/api/timeline/applications/{application_id}/prep").json()

    assert body["status"] == "started"
    assert len(builds) == 1
    assert injected == [sentinel]
    assert len(calls) == 1
    get_settings.cache_clear()


def test_prep_endpoint_explains_reused_refresh_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "true")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        application_id = seed_application(settings.db_path, settings.dev_fake_user)
        assert timeline.claim_prep_generation(
            settings.db_path, settings.dev_fake_user, application_id, "existing"
        )["status"] == "started"
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep?refresh_research=true"
        ).json()

    assert body["status"] == "reused" and body["reused"] is True
    assert body["refresh_requested"] is True and body["refresh_applied"] is False
    assert body["retry_after_seconds"] > 0 and "未另行刷新" in body["message"]
    assert calls == []
    get_settings.cache_clear()


def test_prep_endpoint_takes_over_expired_lease_once(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "true")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        application_id = seed_application(settings.db_path, settings.dev_fake_user)
        assert timeline.claim_prep_generation(
            settings.db_path, settings.dev_fake_user, application_id, "expired"
        )["status"] == "started"
        with transaction(settings.db_path) as conn:
            conn.execute(
                "UPDATE applications SET prep_heartbeat_time = '2000-01-01T00:00:00+00:00' "
                "WHERE id = ?",
                (application_id,),
            )
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep"
            "?force=true&refresh_research=true"
        ).json()

    assert body["status"] == "started" and body["takeover_applied"] is True
    assert body["refresh_applied"] is True and body["reused"] is False
    assert len(calls) == 1 and calls[0][2]["refresh_research"] is True
    get_settings.cache_clear()


def test_takeover_click_does_not_restart_task_that_just_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        application_id = seed_application(settings.db_path, settings.dev_fake_user)
        claim = timeline.claim_prep_generation(
            settings.db_path, settings.dev_fake_user, application_id, "finishing")
        assert claim["status"] == "started"
        assert timeline.set_prep_status(
            settings.db_path,
            settings.dev_fake_user,
            application_id,
            "ready",
            prep={"prepared_time": "now"},
            generation="finishing",
        )
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep"
            "?force=true&refresh_research=true"
        ).json()

    assert body["status"] == "completed" and body["prep_status"] == "ready"
    assert body["takeover_applied"] is False and body["refresh_applied"] is False
    assert calls == []
    get_settings.cache_clear()


def test_takeover_click_retries_task_that_just_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("APP_ALLOW_WEB_RESEARCH", "true")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        application_id = seed_application(settings.db_path, settings.dev_fake_user)
        claim = timeline.claim_prep_generation(
            settings.db_path, settings.dev_fake_user, application_id, "failing")
        assert claim["status"] == "started"
        assert timeline.fail_prep_generation(
            settings.db_path,
            settings.dev_fake_user,
            application_id,
            "旧任务失败",
            generation="failing",
        )
        body = client.post(
            f"/api/timeline/applications/{application_id}/prep"
            "?force=true&refresh_research=true"
        ).json()

    assert body["status"] == "started" and body["takeover_applied"] is False
    assert body["refresh_applied"] is True and len(calls) == 1
    get_settings.cache_clear()


def test_prep_service_build_failure_closes_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)
    with transaction(settings.db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            ('{"unrelated_artifact":{"version":"last-good"}}', application_id),
        )

    def fail_build(_settings, **_kwargs):
        raise RuntimeError("依赖初始化失败")

    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service", fail_build)
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        body = client.post(f"/api/timeline/applications/{application_id}/prep").json()
    assert body["status"] == "error" and "初始化失败" in body["message"]
    assert read_state(settings.db_path, application_id)[0] == "failed"
    with read_connection(settings.db_path) as conn:
        prep_json = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?", (application_id,)
        ).fetchone()[0])
    assert prep_json["unrelated_artifact"] == {"version": "last-good"}
    assert "初始化失败" in prep_json["error"]
    get_settings.cache_clear()


def test_priority_is_pure_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    application_id = seed_application(settings.db_path, settings.dev_fake_user)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        body = client.put(f"/api/timeline/applications/{application_id}/priority",
                          json={"expected_revision": 0, "priority": "high"}).json()
        assert body["id"] == application_id
        assert body["priority"] == "high"
        assert body["revision"] == 1
        assert "timeline_entries" in body
        assert read_state(settings.db_path, application_id) == ("none", "high")

        stale = client.put(
            f"/api/timeline/applications/{application_id}/priority",
            json={"expected_revision": 0, "priority": "low"},
        )
        assert stale.status_code == 409
        assert read_state(settings.db_path, application_id) == ("none", "high")

        body = client.put(f"/api/timeline/applications/{application_id}/priority",
                          json={"expected_revision": 1, "priority": None}).json()
        assert body["priority"] is None
        assert body["revision"] == 2
        assert read_state(settings.db_path, application_id) == ("none", None)

        assert client.put("/api/timeline/applications/9999/priority",
                          json={"expected_revision": 0, "priority": "high"}).status_code == 404
    assert calls == []
    get_settings.cache_clear()


def test_dedicated_resume_upload_does_not_touch_prep(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    user_id = settings.dev_fake_user
    application_id = seed_application(settings.db_path, user_id)
    calls: list = []
    monkeypatch.setattr("careerdesk.orchestration.application_prep.factory.build_prep_service",
                        lambda _settings, **_kwargs: _StubPrepService(calls))
    monkeypatch.setattr(
        "careerdesk.features.resumes.api.build_llm",
        lambda _model, **_kwargs: scripted(RESUME_PARSE, RESUME_PARSE),
    )

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        dedicated = client.post("/api/resumes/upload",
                                files={"file": ("dedicated.md", "# 腾讯专属简历".encode(), "text/markdown")},
                                data={"binding": "application",
                                      "application_id": str(application_id)}).json()
        assert dedicated["status"] == "processing", dedicated
        created_job = client.get("/api/resumes/jobs").json()["items"][0]
        assert created_job["state"] == "completed"
        assert read_state(settings.db_path, application_id) == ("none", None)

        updated = client.put(f"/api/resumes/{created_job['resume_id']}",
                             files={"file": ("v2.md", "# 腾讯专属简历 v2".encode(), "text/markdown")}).json()
        assert updated["status"] == "processing", updated
        updated_job = client.get("/api/resumes/jobs").json()["items"][0]
        assert updated_job["state"] == "completed"
        assert read_state(settings.db_path, application_id) == ("none", None)
    assert calls == []
    get_settings.cache_clear()
