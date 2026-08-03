
import asyncio
import io
import os
import stat
import uuid
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.features.resumes.policy import MAX_RESUME_TEXT_CHARS
from careerdesk.features.resumes.repository import get_resume, upsert_resume
from careerdesk.orchestration.assistant.contracts import CHAT_ATTACHMENTS_TOTAL_CHAR_LIMIT
from careerdesk.platform.database import init_db
from careerdesk.platform.storage.documents import extract_document_text
from careerdesk.platform.http.request_limits import (DEFAULT_JSON_BODY_BYTES, MIB, RequestBodyLimitMiddleware,
                                request_limit)
from careerdesk.platform.storage.private import UnsafeManagedPath
from careerdesk.platform.storage.uploads import (UploadTooLarge, cleanup_stale_files, copy_limited,
                                                save_upload, user_upload_root)


def test_copy_limited_removes_partial_file(tmp_path):
    destination = tmp_path / "partial.bin"
    with pytest.raises(UploadTooLarge):
        copy_limited(io.BytesIO(b"123456"), destination, 5)
    assert not destination.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_copy_limited_creates_private_directories_and_file(tmp_path):
    destination = tmp_path / "uploads" / "user" / "resume.md"
    old_umask = os.umask(0)
    try:
        assert copy_limited(io.BytesIO(b"private resume"), destination, 100) == 14
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE((tmp_path / "uploads").stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_copy_limited_never_follows_or_removes_collision_symlink(tmp_path):
    target = tmp_path / "outside-secret"
    target.write_bytes(b"keep-me")
    root = tmp_path / "uploads"
    root.mkdir()
    destination = root / "collision.md"
    try:
        destination.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(FileExistsError):
        copy_limited(io.BytesIO(b"overwrite"), destination, 100)

    assert destination.is_symlink()
    assert target.read_bytes() == b"keep-me"


def test_chat_upload_limit_and_attachment_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.setattr("careerdesk.orchestration.assistant.service.MAX_CHAT_OR_RESUME_BYTES", 5)
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        preparse = client.post(
            "/api/chat", content=b'{}',
            headers={"content-type": "application/json", "content-length": str(2 * MIB)},
        )
        assert preparse.status_code == 413
        assert preparse.headers["content-type"] == "application/problem+json"
        assert preparse.json()["request_id"] == preparse.headers["x-request-id"]
        too_large = client.post(
            "/api/uploads", files={"file": ("large.png", b"123456", "image/png")})
        assert too_large.status_code == 413
        uploaded = client.post(
            "/api/uploads", files={"file": ("small.png", b"1234", "image/png")}).json()
        assert uploaded["status"] == "ok"
        assert client.delete(f"/api/uploads/{uploaded['stored']}").json() == {"status": "ok"}
        invalid = client.post("/api/chat", json={
            "message": "看附件",
            "session_id": str(uuid.uuid4()),
            "client_turn_id": str(uuid.uuid4()),
            "attachments": [{"kind": "image", "filename": "x.png"}],
        })
        assert invalid.status_code == 422
        mixed_shape = client.post("/api/chat", json={
            "message": "伪装附件",
            "session_id": str(uuid.uuid4()),
            "client_turn_id": str(uuid.uuid4()),
            "attachments": [{
                "kind": "document", "filename": "x.md", "text": "正文", "stored": "x.png",
            }],
        })
        assert mixed_shape.status_code == 422
        noncanonical_turn = client.post("/api/chat", json={
            "message": "非规范编号",
            "session_id": str(uuid.uuid4()),
            "client_turn_id": str(uuid.uuid4()).upper(),
        })
        assert noncanonical_turn.status_code == 422
        too_many = client.post("/api/chat", json={
            "message": "太多附件",
            "session_id": str(uuid.uuid4()),
            "client_turn_id": str(uuid.uuid4()),
            "attachments": [
                {"kind": "document", "filename": f"{index}.md", "text": "x"}
                for index in range(9)
            ],
        })
        assert too_many.status_code == 422
        too_much_text = client.post("/api/chat", json={
            "message": "文档太多",
            "session_id": str(uuid.uuid4()),
            "client_turn_id": str(uuid.uuid4()),
            "attachments": [
                {
                    "kind": "document",
                    "filename": f"{index}.md",
                    "text": "x" * (CHAT_ATTACHMENTS_TOTAL_CHAR_LIMIT // 3 + 1),
                }
                for index in range(3)
            ],
        })
        assert too_much_text.status_code == 422

    assert not list((tmp_path / "data" / "uploads").rglob("*.png"))
    get_settings.cache_clear()


def test_upload_quota_and_stale_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    first = save_upload(io.BytesIO(b"1234"), "first.png", root, 10, max_total_bytes=6)
    with pytest.raises(UploadTooLarge, match="存储额度"):
        save_upload(io.BytesIO(b"5678"), "second.png", root, 10, max_total_bytes=6)
    assert first.exists() and len(list(root.iterdir())) == 1

    monkeypatch.setattr("careerdesk.platform.storage.uploads.time", lambda: 10_000)
    first.touch()
    os.utime(first, (1, 1))
    assert cleanup_stale_files(root, max_age_seconds=100) == 1
    assert not first.exists()


def test_archiving_resume_frees_managed_original_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    root = user_upload_root(Path(settings.db_path).parent, "resumes", settings.dev_fake_user)
    root.mkdir(parents=True, exist_ok=True)
    original = root / "resume.md"
    original.write_text("resume", encoding="utf-8")
    resume_id = upsert_resume(
        settings.db_path, settings.dev_fake_user, "主简历", "resume",
        file_path=str(original), lines=[])

    from careerdesk.bootstrap.app import create_app
    with TestClient(create_app()) as client:
        response = client.delete(f"/api/resumes/{resume_id}")

    assert response.json()["status"] == "ok"
    assert not original.exists()
    get_settings.cache_clear()


def test_default_api_body_limit_preserves_multipart_overrides():
    assert request_limit("PUT", "/api/questions/1/answer", "application/json") == DEFAULT_JSON_BODY_BYTES
    assert request_limit("POST", "/api/future", "") == DEFAULT_JSON_BODY_BYTES
    assert request_limit("POST", "/api/future", "multipart/form-data; boundary=x") is None
    assert request_limit("POST", "/api/retired/upload", "multipart/form-data") is None
    assert request_limit("POST", "/api/resumes/upload", "multipart/form-data") == 11 * MIB
    assert request_limit("POST", "/not-api", "application/json") is None


def test_body_limit_counts_chunked_request_without_content_length(monkeypatch):
    monkeypatch.setattr("careerdesk.platform.http.request_limits.DEFAULT_JSON_BODY_BYTES", 5)
    received_by_app: list[bytes] = []
    sent: list[dict] = []
    messages = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
    ])

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def downstream(_scope, limited_receive, _send):
        while True:
            message = await limited_receive()
            received_by_app.append(message.get("body", b""))
            if not message.get("more_body"):
                return

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/api/questions/1/answer",
        "headers": [(b"content-type", b"application/json")],
    }
    asyncio.run(RequestBodyLimitMiddleware(downstream)(scope, receive, send))

    assert received_by_app == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_chunked_body_replay_delegates_disconnect_after_eof(monkeypatch):
    monkeypatch.setattr("careerdesk.platform.http.request_limits.DEFAULT_JSON_BODY_BYTES", 10)
    received_by_app: list[dict] = []
    messages = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
        {"type": "http.disconnect"},
    ])

    async def receive():
        return next(messages)

    async def send(_message):
        raise AssertionError("downstream 不应发送响应")

    async def downstream(_scope, replay_receive, _send):
        while True:
            message = await replay_receive()
            received_by_app.append(message)
            if message["type"] == "http.request" and not message.get("more_body"):
                break
        received_by_app.append(await replay_receive())

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/api/questions/1/answer",
        "headers": [(b"content-type", b"application/json")],
    }
    asyncio.run(RequestBodyLimitMiddleware(downstream)(scope, receive, send))

    assert received_by_app == [
        {"type": "http.request", "body": b"123456", "more_body": False},
        {"type": "http.disconnect"},
    ]


def test_chunked_body_limit_returns_413_through_fastapi(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.setattr("careerdesk.platform.http.request_limits.DEFAULT_JSON_BODY_BYTES", 5)
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        response = client.put(
            "/api/questions/1/answer",
            content=iter([b'{"a"', b':"123456"}']),
            headers={"content-type": "application/json"},
        )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    get_settings.cache_clear()


def test_json_model_text_boundaries_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        model = client.put("/api/settings", json={"llm_model": "x" * 513})
        key = client.put("/api/settings", json={"keys": {"OPENAI_API_KEY": "x" * 513}})
        resume = client.post("/api/resumes", json={
            "name": "过长简历", "content_text": "x" * (MAX_RESUME_TEXT_CHARS + 1),
        })

    assert {model.status_code, key.status_code, resume.status_code} == {422}
    get_settings.cache_clear()


def test_resume_upload_rejects_derived_long_name_before_model_check(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/resumes/upload",
            files={"file": (f"{'x' * 201}.md", b"resume", "text/markdown")},
        )

    assert response.status_code == 422
    assert "200" in response.json()["detail"]
    get_settings.cache_clear()


def test_resume_upload_preflights_llm_then_cleans_rejected_extracted_text(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    calls = []
    monkeypatch.setattr(
        "careerdesk.features.resumes.api.extract_document_text",
        lambda _path: calls.append("extract") or "x" * (MAX_RESUME_TEXT_CHARS + 1),
    )
    monkeypatch.setattr(
        "careerdesk.features.resumes.api.build_llm",
        lambda _model, **_kwargs: calls.append("build") or object(),
    )
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/resumes/upload",
            files={"file": ("resume.md", b"small source", "text/markdown")},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    with TestClient(create_app()) as client:
        jobs = client.get("/api/resumes/jobs").json()["items"]
    assert jobs[0]["state"] == "failed"
    assert "200,000" in jobs[0]["message"]
    assert calls == ["build", "extract"]
    assert not list((tmp_path / "data" / "uploads").rglob("*.md"))
    get_settings.cache_clear()


def test_strict_cloud_resume_upload_and_update_reject_before_managed_disk_write(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "openai:gpt-4o-mini")
    monkeypatch.setenv("APP_STRICT_OFFLINE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "configured-but-dormant")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)
    resume_id = upsert_resume(
        settings.db_path,
        settings.dev_fake_user,
        "现有简历",
        "旧内容",
    )

    def forbidden_parser(_path):
        raise AssertionError("严格离线拒绝必须早于文档解析")

    monkeypatch.setattr(
        "careerdesk.features.resumes.api.extract_document_text",
        forbidden_parser,
    )
    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        created = client.post(
            "/api/resumes/upload",
            files={"file": ("blocked-new.md", b"new", "text/markdown")},
        )
        updated = client.put(
            f"/api/resumes/{resume_id}",
            files={"file": ("blocked-update.md", b"update", "text/markdown")},
        )

    assert created.status_code == updated.status_code == 409
    assert created.json()["code"] == updated.json()["code"] == "strict_offline"
    assert not [path for path in (tmp_path / "data" / "uploads").rglob("*") if path.is_file()]
    assert get_resume(settings.db_path, settings.dev_fake_user, resume_id)["content_text"] == "旧内容"
    get_settings.cache_clear()


def test_docx_preflight_rejects_large_uncompressed_archive(tmp_path, monkeypatch):
    path = tmp_path / "bomb.docx"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"123456")
    monkeypatch.setattr("careerdesk.platform.storage.documents.MAX_DOCX_UNCOMPRESSED_BYTES", 5)

    with pytest.raises(ValueError, match="解压后内容过大"):
        extract_document_text(str(path))


def test_local_docx_conversion_receives_fail_closed_network_session(tmp_path, monkeypatch):
    path = tmp_path / "local.docx"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<document>safe local text</document>")
    captured = {}

    class FakeMarkItDown:
        def __init__(self, *, requests_session, enable_plugins):
            captured["session"] = requests_session
            assert enable_plugins is False

        @staticmethod
        def convert(_path):
            class Result:
                text_content = "safe local text"

            return Result()

    monkeypatch.setattr("markitdown.MarkItDown", FakeMarkItDown)

    assert extract_document_text(str(path)) == "safe local text"
    with pytest.raises(RuntimeError, match="禁止网络"):
        captured["session"].get("https://canary.invalid")


def test_document_parser_error_does_not_expose_internal_paths(tmp_path, monkeypatch):
    path = tmp_path / "private-resume.docx"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<document />")

    class BrokenMarkItDown:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def convert(_path):
            raise RuntimeError("model missing at /Users/private/CareerDesk.app/secret")

    monkeypatch.setattr("markitdown.MarkItDown", BrokenMarkItDown)

    with pytest.raises(ValueError) as captured:
        extract_document_text(str(path))

    assert "无法读取这个文件" in str(captured.value)
    assert "/Users/private" not in str(captured.value)
    assert "secret" not in str(captured.value)


def test_pdf_preflight_rejects_excessive_page_count(tmp_path, monkeypatch):
    path = tmp_path / "many.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr("careerdesk.platform.storage.documents.MAX_PDF_PAGES", 2)
    monkeypatch.setattr(
        "pdfminer.pdfpage.PDFPage.get_pages",
        lambda *_args, **_kwargs: iter([object(), object(), object()]),
    )

    with pytest.raises(ValueError, match="PDF 页数过多"):
        extract_document_text(str(path))


def test_user_upload_roots_do_not_reveal_or_share_identifiers(tmp_path):
    alice = user_upload_root(tmp_path, "chat", "alice@example.com")
    bob = user_upload_root(tmp_path, "chat", "bob@example.com")
    assert alice != bob
    assert "alice" not in str(alice) and "bob" not in str(bob)
    assert alice.parent == bob.parent


@pytest.mark.parametrize("category", ["", ".", "..", " chat", "chat ", "a/b", r"a\b", "a\x00b"])
def test_user_upload_root_requires_one_category_component(tmp_path, category):
    with pytest.raises(ValueError, match="非法上传目录"):
        user_upload_root(tmp_path, category, "u1")


@pytest.mark.parametrize("linked_layer", ["uploads", "category", "user"])
def test_user_upload_root_rejects_internal_symlink_layers(tmp_path, linked_layer):
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    if linked_layer == "uploads":
        (data / "uploads").symlink_to(outside, target_is_directory=True)
    elif linked_layer == "category":
        (data / "uploads").mkdir()
        (data / "uploads" / "chat").symlink_to(outside, target_is_directory=True)
    else:
        user = user_upload_root(data, "chat", "u1")
        user.rmdir()
        user.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeManagedPath, match="符号链接"):
        user_upload_root(data, "chat", "u1")
