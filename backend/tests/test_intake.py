
import asyncio
import json
from pathlib import Path

import pytest
from agentmaker import Hook
from tests.support import ScriptedLLM
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.resumes.repository import archive_resume, get_resume, upsert_resume
from careerdesk.orchestration.assistant.service import run_chat
from careerdesk.platform.storage.uploads import user_upload_root

TODAY = "2026-07-07"

def scripted(*payloads) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(payload, ensure_ascii=False) for payload in payloads])


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    path = get_settings().db_path
    init_db(path)
    yield path
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_upload_chat_attachment_kinds(client, tmp_path):
    test_client, = (client[0],) if isinstance(client, tuple) else (client,)
    jd = tmp_path / "jd.md"
    jd.write_text("岗位要求：熟悉 RAG 与 Agent 工程", encoding="utf-8")
    with jd.open("rb") as f:
        document = test_client.post("/api/uploads", files={"file": ("jd.md", f, "text/markdown")}).json()
    assert document["kind"] == "document" and "RAG" in document["text"]

    image = test_client.post("/api/uploads",
                             files={"file": ("shot.png", b"\x89PNG\r\n\x1a\nfake", "image/png")}).json()
    assert image["kind"] == "image"

    bad = test_client.post("/api/uploads", files={"file": ("x.exe", b"MZ", "application/x-msdownload")}).json()
    assert bad["status"] == "error"


def test_chat_document_attachment_injected_into_message(db_path):
    seen: list = []

    class CaptureHook(Hook):
        def before_model(self, messages):
            seen.append(messages[-1]["content"])

    def factory(hooks):
        from careerdesk.agentic.agents import build_career_assistant
        return build_career_assistant(
            db_path,
            ScriptedLLM(["看到了"]),
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000102",
            trusted_review_source="帮我看看这个 JD",
            hooks=[*hooks, CaptureHook()],
        )

    async def collect():
        return [event async for event in run_chat(
            "帮我看看这个 JD", "document-session", "u1",
            client_turn_id="document-turn",
            attachments=[{"kind": "document", "filename": "jd.md", "text": "岗位要求：熟悉 RAG"}],
            agent_factory=factory)]

    events = run(collect())
    assert any(event.event == "done" for event in events)
    assert seen and "[附件：jd.md]" in seen[-1] and "熟悉 RAG" in seen[-1]


def test_supports_image_input_by_provider_capability():
    from careerdesk.platform.ai.client import supports_image_input

    refused, hint = supports_image_input("deepseek:deepseek-chat", strict_offline=False)
    assert refused is False and "不支持图片输入" in hint and "模型与隐私" in hint
    assert supports_image_input("openai:gpt-4o-mini", strict_offline=False) == (True, "")
    assert supports_image_input(None, strict_offline=False)[0] is False


def test_chat_image_attachment_gets_capability_hint(db_path, monkeypatch):
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    called = []

    def factory(hooks):
        called.append(True)
        raise AssertionError("图片提示路径不应构建 agent")

    async def collect():
        return [event async for event in run_chat(
            "看下这张截图", "hint-session", "u1", client_turn_id="hint-turn",
            attachments=[{"kind": "image", "filename": "shot.png"}], agent_factory=factory)]

    events = run(collect())
    get_settings.cache_clear()
    assert not called
    assert [event.event for event in events] == ["error"]
    assert events[0].data["code"] == "image_unsupported"
    assert "不支持图片输入" in events[0].data["message"]


def test_chat_image_attachment_builds_multimodal_parts(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_LLM_MODEL", "openai:gpt-4o-mini")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    uploads = user_upload_root(tmp_path / "data", "chat", "u1")
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "ab12_shot.png").write_bytes(b"\x89PNG\r\n\x1a\npng-bytes")

    from agentmaker import Hook
    seen: list = []

    class CaptureHook(Hook):
        def before_model(self, messages):
            seen.append(messages[-1]["content"])

    def factory(hooks):
        from careerdesk.agentic.agents import build_career_assistant
        return build_career_assistant(
            db_path,
            ScriptedLLM(["看到图了"]),
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000103",
            trusted_review_source="这是啥",
            hooks=[*hooks, CaptureHook()],
        )

    async def collect(attachments, turn_id):
        return [event async for event in run_chat(
            "这是啥", "multimodal-session", "u1", client_turn_id=turn_id,
            attachments=attachments, agent_factory=factory,
        )]

    events = run(collect(
        [{"kind": "image", "filename": "shot.png", "stored": "ab12_shot.png"}],
        "multimodal-ok",
    ))
    assert "".join(e.data["text"] for e in events if e.event == "message_delta") == "看到图了"
    payload = seen[-1]
    assert isinstance(payload, list) and payload[0]["type"] == "image"
    assert payload[-1]["type"] == "text" and "这是啥" in payload[-1]["text"]

    events = run(collect(
        [{"kind": "image", "filename": "x.png", "stored": "../../etc/passwd"}],
        "multimodal-invalid",
    ))
    get_settings.cache_clear()
    assert events[-1].event == "error" and events[-1].data["code"] == "attachment_invalid"
    assert len(seen) == 1


def test_update_resume_replaces_content_keeps_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    db_path = settings.db_path
    init_db(db_path)
    user_id = settings.dev_fake_user

    resume_id = upsert_resume(db_path, user_id, "通用简历", "旧内容 v1", family="backend",
                              binding="family", lines=[{"text": "旧行", "knowledge_points": []}])
    parse = {"family": "backend", "lines": [{
        "line_index": 1, "knowledge_points": ["RAG"],
    }]}
    monkeypatch.setattr(
        "careerdesk.features.resumes.api.build_llm",
        lambda _model, **_kwargs: scripted(parse),
    )

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        response = test_client.put(f"/api/resumes/{resume_id}",
                                   files={"file": ("new.md", b"# new content\n- new stuff", "text/markdown")})
    body = response.json()
    assert body["status"] == "processing", body

    updated = get_resume(db_path, user_id, resume_id)
    assert "new content" in updated["content_text"]
    assert updated["binding"] == "family" and updated["family"] == "backend"
    assert [line["text"] for line in updated["lines"]] == [
        "# new content",
        "- new stuff",
    ]
    get_settings.cache_clear()


def test_update_resume_invalidates_dependent_prep_but_keeps_unrelated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    db_path = settings.db_path
    user_id = settings.dev_fake_user
    init_db(db_path)

    target_id = upsert_resume(
        db_path, user_id, "待更新通用版", "old", family="backend",
        lines=[{"text": "old", "knowledge_points": []}],
    )
    other_id = upsert_resume(
        db_path, user_id, "另一版本", "other", family="algorithm",
        lines=[{"text": "other", "knowledge_points": []}],
    )
    assert target_id is not None and other_id is not None

    old_prep = {
        "research": "ok",
        "prepared_time": "2026-07-01T00:00:00+00:00",
        "resume_adaptation": {"input_hash": "old-adaptation"},
        "resume_adaptation_summary": {"resume_content_hash": "old-resume"},
        "nontech_answers": [{"question": "为什么来？", "answer": "基于旧简历"}],
        "unrelated_cache": {"keep": True},
    }
    with transaction(db_path) as conn:
        for position, bound_resume_id in (
            ("显式绑定岗", target_id),
            ("未绑定兜底岗", None),
            ("绑定其他版本岗", other_id),
        ):
            conn.execute(
                "INSERT INTO applications (user_id, company, position, resume_id, prep_status, "
                "prep_json, created_time, updated_time) VALUES (?, '测试公司', ?, ?, 'ready', ?, ?, ?)",
                (user_id, position, bound_resume_id, json.dumps(old_prep, ensure_ascii=False),
                 now_iso(), now_iso()),
            )

    parse = {"family": "backend",
             "lines": [{"line_index": 0, "knowledge_points": ["RAG"]}]}
    llm_builds: list[str] = []

    def build_once(model: str, **_kwargs):
        llm_builds.append(model)
        return scripted(parse)

    monkeypatch.setattr("careerdesk.features.resumes.api.build_llm", build_once)

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        body = test_client.put(
            f"/api/resumes/{target_id}",
            files={"file": ("new.md", b"new resume content", "text/markdown")},
        ).json()
    assert body["status"] == "processing", body
    assert llm_builds == ["deepseek:deepseek-chat"]

    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT position, prep_status, prep_json FROM applications "
            "WHERE user_id = ? ORDER BY position",
            (user_id,),
        ).fetchall()
    states = {position: (status, json.loads(prep_json))
              for position, status, prep_json in rows}
    dependent_keys = {
        "resume_adaptation",
        "resume_adaptation_summary",
    }
    for position in ("显式绑定岗", "未绑定兜底岗"):
        status, prep = states[position]
        assert status == "ready"
        assert dependent_keys.isdisjoint(prep)
        assert prep["research"] == "ok" and prep["unrelated_cache"] == {"keep": True}
        assert prep["nontech_answers"] == old_prep["nontech_answers"]
        assert prep["prepared_time"] == "2026-07-01T00:00:00+00:00"

    unaffected_status, unaffected_prep = states["绑定其他版本岗"]
    assert unaffected_status == "ready"
    assert unaffected_prep == old_prep
    get_settings.cache_clear()


def test_update_resume_keeps_new_file_when_old_cleanup_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    user_id = settings.dev_fake_user
    root = user_upload_root(Path(settings.db_path).parent, "resumes", user_id)
    root.mkdir(parents=True, exist_ok=True)
    old_file = root / "old.md"
    old_file.write_text("old", encoding="utf-8")
    resume_id = upsert_resume(
        settings.db_path,
        user_id,
        "通用简历",
        "old",
        family="backend",
        file_path=str(old_file),
        lines=[{"text": "old", "knowledge_points": []}],
    )
    parse = {"family": "backend", "lines": [{"line_index": 0,
                                                "knowledge_points": ["RAG"]}]}
    monkeypatch.setattr(
        "careerdesk.features.resumes.api.build_llm",
        lambda _model, **_kwargs: scripted(parse),
    )
    original_unlink = Path.unlink

    def fail_old_unlink(path: Path, *args, **kwargs):
        if path == old_file:
            raise PermissionError("old file is locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_unlink)
    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        body = test_client.put(
            f"/api/resumes/{resume_id}",
            files={"file": ("new.md", b"new resume", "text/markdown")},
        ).json()
        job = test_client.get("/api/resumes/jobs").json()["items"][0]

    assert body["status"] == "processing"
    assert job["state"] == "completed"
    assert "old file is locked" in job["message"]
    with read_connection(settings.db_path) as conn:
        (new_path,) = conn.execute(
            "SELECT file_path FROM resumes WHERE user_id = ? AND id = ?", (user_id, resume_id)
        ).fetchone()
    new_file = Path(new_path)
    assert new_file.exists() and new_file != old_file
    assert old_file.exists()
    get_settings.cache_clear()


def test_update_resume_never_follows_old_file_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    user_id = settings.dev_fake_user
    parse = {"family": "backend", "lines": [{"line_index": 0,
                                                "knowledge_points": ["RAG"]}]}
    monkeypatch.setattr(
        "careerdesk.features.resumes.api.build_llm",
        lambda _model, **_kwargs: scripted(parse),
    )

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        # Startup intentionally rejects pre-existing upload links.  Insert this
        # hostile legacy path after startup to independently verify the runtime
        # replacement cleanup still never follows it.
        root = user_upload_root(Path(settings.db_path).parent, "resumes", user_id)
        target = tmp_path / "outside.md"
        target.write_text("must survive", encoding="utf-8")
        old_link = root / "old-link.md"
        old_link.symlink_to(target)
        resume_id = upsert_resume(
            settings.db_path,
            user_id,
            "通用简历",
            "old",
            family="backend",
            file_path=str(old_link),
            lines=[{"text": "old", "knowledge_points": []}],
        )
        body = test_client.put(
            f"/api/resumes/{resume_id}",
            files={"file": ("new.md", b"new resume", "text/markdown")},
        ).json()

    assert body["status"] == "processing"
    assert target.read_text(encoding="utf-8") == "must survive"
    assert old_link.is_symlink()
    with read_connection(settings.db_path) as conn:
        (new_path,) = conn.execute(
            "SELECT file_path FROM resumes WHERE user_id = ? AND id = ?", (user_id, resume_id)
        ).fetchone()
    assert Path(new_path).exists()
    get_settings.cache_clear()


def test_duplicate_resume_posts_do_not_overwrite_or_do_work(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    user_id = settings.dev_fake_user
    resume_id = upsert_resume(
        settings.db_path, user_id, "通用简历", "不可覆盖的旧内容", family="backend",
        lines=[{"text": "旧行", "knowledge_points": []}],
    )
    assert resume_id is not None

    def forbidden_llm(_model):
        raise AssertionError("同名 POST 不应构建或调用 LLM")

    def forbidden_save(_file, _subdirectory):
        raise AssertionError("同名上传不应保存文件")

    monkeypatch.setattr("careerdesk.features.resumes.api.build_llm", forbidden_llm)
    monkeypatch.setattr("careerdesk.features.resumes.api._save_upload", forbidden_save)

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as test_client:
        pasted = test_client.post(
            "/api/resumes",
            json={"name": "通用简历", "content_text": "试图覆盖 active 版本"},
        ).json()
        assert pasted == {"status": "error", "message": "同名已存在，请改版本名或使用更新"}

        assert archive_resume(settings.db_path, user_id, resume_id)
        uploaded = test_client.post(
            "/api/resumes/upload",
            files={"file": ("new.md", b"attempted replacement", "text/markdown")},
            data={"name": "通用简历"},
        ).json()
        assert uploaded == {"status": "error", "message": "同名已存在，请改版本名或使用更新"}

    unchanged = get_resume(settings.db_path, user_id, resume_id)
    assert unchanged is not None
    assert unchanged["content_text"] == "不可覆盖的旧内容"
    assert unchanged["archived"] is True
    assert [line["text"] for line in unchanged["lines"]] == ["旧行"]
    with read_connection(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resumes WHERE user_id = ?", (user_id,)).fetchone()[0] == 1
    get_settings.cache_clear()
