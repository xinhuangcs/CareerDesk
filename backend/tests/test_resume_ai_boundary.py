
import asyncio
import json
from pathlib import Path

import pytest
from tests.support import ScriptedLLM
from fastapi.testclient import TestClient

from careerdesk.core.config import get_settings
from careerdesk.features.resumes import ai_tasks
from careerdesk.features.resumes.repository import (
    archive_resume,
    get_resume,
    upsert_resume,
)
from careerdesk.features.resumes.policy import MAX_RESUME_SOURCE_SEGMENTS
from careerdesk.features.resumes.service import ResumeService
from careerdesk.platform.ai.structured_tasks import (
    INSUFFICIENT_CONTEXT,
    StructuredTaskCapacityError,
)
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.platform.storage.uploads import user_upload_root


def run(coroutine):
    return asyncio.run(coroutine)


def scripted(payload: dict) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(payload, ensure_ascii=False)])


@pytest.fixture
def resume_db(tmp_path) -> str:
    db_path = str(tmp_path / "resumes-safety.db")
    init_db(db_path)
    return db_path


def _resume_count(db_path: str) -> int:
    with read_connection(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]


def _valid_parse(*, line_index: int = 0) -> dict:
    return {
        "family": "backend",
        "lines": [{
            "line_index": line_index,
            "knowledge_points": ["幂等"],
        }],
    }


def test_blank_source_stops_before_model_or_database(resume_db):
    service = ResumeService(resume_db, ScriptedLLM([]))

    blank = run(service.register("u1", "空白", " \n\t "))

    assert blank == {"status": "error", "message": "简历文本不能为空"}
    assert _resume_count(resume_db) == 0


@pytest.mark.parametrize(
    ("content", "expected_lengths"),
    [
        ("甲" * 2_500 + "。" + "乙" * 2_500, [2_501, 2_500]),
        ("x" * 4_001, [4_000, 1]),
    ],
)
def test_overlong_extracted_line_is_deterministically_segmented(
    resume_db,
    content,
    expected_lengths,
):
    output = {
        "family": "backend",
        "lines": [
            {"line_index": 0, "knowledge_points": ["分段"]},
            {"line_index": 1, "knowledge_points": ["边界"]},
        ],
    }

    result = run(ResumeService(resume_db, scripted(output)).register(
        "u1",
        "长行 PDF",
        content,
    ))

    assert result["status"] == "ok" and result["line_count"] == 2
    stored = get_resume(resume_db, "u1", result["resume_id"])
    assert stored["content_text"] == content
    assert [len(line["text"]) for line in stored["lines"]] == expected_lengths
    assert "".join(line["text"] for line in stored["lines"]) == content


@pytest.mark.parametrize(
    ("cluster", "expected_prefix"),
    [
        ("e\u0301", "e\u0301"),
        ("✈\ufe0f", "✈\ufe0f"),
        ("🇩🇰", "🇩🇰"),
        ("👨\u200d👩\u200d👧\u200d👦", "👨\u200d👩\u200d👧\u200d👦"),
    ],
)
def test_overlong_line_hard_cut_keeps_common_unicode_clusters_together(
    resume_db,
    cluster,
    expected_prefix,
):
    content = "x" * 3_999 + cluster + "y" * 20
    output = {
        "family": "backend",
        "lines": [
            {"line_index": 0, "knowledge_points": ["前段"]},
            {"line_index": 1, "knowledge_points": ["后段"]},
        ],
    }

    result = run(ResumeService(resume_db, scripted(output)).register(
        "u1",
        "Unicode 长行",
        content,
    ))

    assert result["status"] == "ok" and result["line_count"] == 2
    stored = get_resume(resume_db, "u1", result["resume_id"])
    texts = [line["text"] for line in stored["lines"]]
    assert max(map(len, texts)) <= 4_000
    assert texts[0] == "x" * 3_999
    assert texts[1].startswith(expected_prefix)
    assert "".join(texts) == content


def test_full_visible_resume_lines_are_stored_beyond_ai_highlight_limit(resume_db):
    visible_lines = [f"第 {index:03d} 段可见文字" for index in range(120)]
    output = {
        "family": "backend",
        "lines": [
            {"line_index": 3, "knowledge_points": ["缓存"]},
            {"line_index": 117, "knowledge_points": ["事务"]},
        ],
    }

    result = run(ResumeService(resume_db, scripted(output)).register(
        "u1",
        "完整全文",
        "\n".join(visible_lines),
    ))

    assert result["status"] == "ok"
    assert result["line_count"] == len(visible_lines)
    stored = get_resume(resume_db, "u1", result["resume_id"])
    assert [line["text"] for line in stored["lines"]] == visible_lines
    assert stored["lines"][3]["knowledge_points"] == ["缓存"]
    assert stored["lines"][117]["knowledge_points"] == ["事务"]
    assert all(
        line["knowledge_points"] == []
        for index, line in enumerate(stored["lines"])
        if index not in {3, 117}
    )


def test_pathological_visible_segment_count_fails_without_model_or_write(resume_db):
    content = "\n".join("x" for _ in range(MAX_RESUME_SOURCE_SEGMENTS + 1))
    result = run(ResumeService(resume_db, ScriptedLLM([])).register(
        "u1",
        "异常逐字换行",
        content,
    ))

    assert result["status"] == "error"
    assert f"{MAX_RESUME_SOURCE_SEGMENTS:,}" in result["message"]
    assert "可见段落过多" in result["message"]
    assert _resume_count(resume_db) == 0


def test_parse_materializes_only_canonical_source_lines(resume_db):
    content = "  真实第一行  \n真实第二行"
    output = {
        "family": "backend",
        "lines": [
            {
                "line_index": 0,
                "knowledge_points": ["幂等"],
            },
            {
                "line_index": 1,
                "knowledge_points": ["RAG"],
            },
        ],
    }

    result = run(ResumeService(resume_db, scripted(output)).register(
        "u1", "可信版", content,
    ))

    assert result["status"] == "ok" and result["line_count"] == 2
    stored = get_resume(resume_db, "u1", result["resume_id"])
    assert stored["lines"] == [
        {"text": "真实第一行", "knowledge_points": ["幂等"]},
        {"text": "真实第二行", "knowledge_points": ["RAG"]},
    ]


def test_out_of_range_annotation_does_not_discard_canonical_resume(resume_db):
    result = run(ResumeService(resume_db, scripted(_valid_parse(line_index=99))).register(
        "u1", "不得落库", "唯一真实行",
    ))

    assert result["status"] == "ok" and result["line_count"] == 0
    assert _resume_count(resume_db) == 1
    assert get_resume(resume_db, "u1", result["resume_id"])["annotation_status"] == "pending"


def test_identical_text_at_different_source_indexes_is_not_merged(resume_db):
    content = "优化性能\n优化性能"
    output = {
        "family": "backend",
        "lines": [
            {"line_index": 0, "knowledge_points": ["缓存"]},
            {"line_index": 1, "knowledge_points": ["并发"]},
        ],
    }

    result = run(ResumeService(resume_db, scripted(output)).register(
        "u1", "同文不同行", content,
    ))

    assert result["status"] == "ok" and result["line_count"] == 2
    assert get_resume(resume_db, "u1", result["resume_id"])["lines"] == [
        {"text": "优化性能", "knowledge_points": ["缓存"]},
        {"text": "优化性能", "knowledge_points": ["并发"]},
    ]


def test_annotation_capacity_failure_keeps_canonical_resume(resume_db, monkeypatch):
    async def fail_capacity(*args, **kwargs):
        raise StructuredTaskCapacityError(INSUFFICIENT_CONTEXT)

    monkeypatch.setattr(ai_tasks, "run_structured_task", fail_capacity)
    result = run(ResumeService(resume_db, object()).register(
        "u1", "容量失败", "真实简历行",
    ))

    assert result["status"] == "ok" and result["line_count"] == 0
    assert _resume_count(resume_db) == 1


def test_late_update_loses_full_snapshot_cas_even_when_timestamp_is_reused(resume_db):
    resume_id = upsert_resume(
        resume_db,
        "u1",
        "主简历",
        "旧内容",
        family="backend",
        lines=[{"text": "旧内容", "knowledge_points": ["旧"]}],
    )
    assert resume_id is not None
    with read_connection(resume_db) as conn:
        (original_time,) = conn.execute(
            "SELECT updated_time FROM resumes WHERE id = ?", (resume_id,)
        ).fetchone()

    class SameTimestampMutatingLLM(ScriptedLLM):
        def __init__(self):
            super().__init__([json.dumps(_valid_parse(), ensure_ascii=False)])

        async def chat(self, messages, **kwargs):
            with transaction(resume_db) as conn:
                conn.execute(
                    "UPDATE resumes SET content_text = '较新的用户内容', "
                    "lines_json = ?, updated_time = ? WHERE id = ?",
                    (
                        json.dumps([{
                            "text": "较新的用户内容",

                            "knowledge_points": ["新"],
                        }], ensure_ascii=False),
                        original_time,
                        resume_id,
                    ),
                )
            return await super().chat(messages, **kwargs)

    result = run(ResumeService(resume_db, SameTimestampMutatingLLM()).register(
        "u1",
        "主简历",
        "晚到的旧请求内容",
        family="backend",
        replace_existing=True,
    ))

    assert result["status"] == "stale"
    stored = get_resume(resume_db, "u1", resume_id)
    assert stored["content_text"] == "较新的用户内容"
    assert stored["lines"][0]["text"] == "较新的用户内容"


def test_update_cannot_revive_resume_archived_while_model_is_running(resume_db):
    resume_id = upsert_resume(
        resume_db,
        "u1",
        "主简历",
        "旧内容",
        family="backend",
        lines=[{"text": "旧内容", "knowledge_points": ["旧"]}],
    )
    assert resume_id is not None

    class ArchivingLLM(ScriptedLLM):
        def __init__(self):
            super().__init__([json.dumps(_valid_parse(), ensure_ascii=False)])

        async def chat(self, messages, **kwargs):
            assert archive_resume(resume_db, "u1", resume_id)
            return await super().chat(messages, **kwargs)

    result = run(ResumeService(resume_db, ArchivingLLM()).register(
        "u1",
        "主简历",
        "晚到的替换内容",
        family="backend",
        replace_existing=True,
    ))

    assert result["status"] == "stale"
    stored = get_resume(resume_db, "u1", resume_id)
    assert stored["archived"] is True
    assert stored["content_text"] == "旧内容"

    skipped = run(ResumeService(resume_db, ScriptedLLM([])).register(
        "u1",
        "主简历",
        "不得复活",
        family="backend",
        replace_existing=True,
    ))
    assert skipped["status"] == "stale"
    assert get_resume(resume_db, "u1", resume_id)["archived"] is True


def test_late_content_update_never_steals_newer_application_resume_selection(resume_db):
    with transaction(resume_db) as conn:
        application_id = conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'C', 'P', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
    first_id = upsert_resume(
        resume_db,
        "u1",
        "专属 A",
        "A 旧内容",
        family="backend",
        binding="application",
        application_id=application_id,
        lines=[{"text": "A 旧内容", "knowledge_points": ["旧"]}],
    )
    assert first_id is not None
    second_id: int | None = None

    class SelectingNewResumeLLM(ScriptedLLM):
        def __init__(self):
            super().__init__([json.dumps(_valid_parse(), ensure_ascii=False)])

        async def chat(self, messages, **kwargs):
            nonlocal second_id
            second_id = upsert_resume(
                resume_db,
                "u1",
                "专属 B",
                "B 新内容",
                family="backend",
                binding="application",
                application_id=application_id,
                lines=[{
                    "text": "B 新内容",

                    "knowledge_points": ["新"],
                }],
            )
            return await super().chat(messages, **kwargs)

    result = run(ResumeService(resume_db, SelectingNewResumeLLM()).register(
        "u1",
        "专属 A",
        "A 更新内容",
        family="backend",
        binding="application",
        application_id=application_id,
        replace_existing=True,
    ))

    assert result["status"] == "ok"
    assert get_resume(resume_db, "u1", first_id)["content_text"] == "A 更新内容"
    with read_connection(resume_db) as conn:
        (selected_id,) = conn.execute(
            "SELECT resume_id FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
    assert second_id is not None and selected_id == second_id


def test_application_delete_during_parse_is_reported_as_stale(resume_db):
    with transaction(resume_db) as conn:
        application_id = conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', 'C', 'P', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
    resume_id = upsert_resume(
        resume_db,
        "u1",
        "专属 A",
        "A 旧内容",
        family="backend",
        binding="application",
        application_id=application_id,
        lines=[{"text": "A 旧内容", "knowledge_points": ["旧"]}],
    )
    assert resume_id is not None

    class DeletingApplicationLLM(ScriptedLLM):
        def __init__(self):
            super().__init__([json.dumps(_valid_parse(), ensure_ascii=False)])

        async def chat(self, messages, **kwargs):
            with transaction(resume_db) as conn:
                conn.execute(
                    "UPDATE resumes SET application_id = NULL, binding = 'family' "
                    "WHERE user_id = 'u1' AND id = ?",
                    (resume_id,),
                )
                conn.execute(
                    "DELETE FROM applications WHERE user_id = 'u1' AND id = ?",
                    (application_id,),
                )
            return await super().chat(messages, **kwargs)

    result = run(ResumeService(resume_db, DeletingApplicationLLM()).register(
        "u1",
        "专属 A",
        "A 更新内容",
        family="backend",
        binding="application",
        application_id=application_id,
        replace_existing=True,
    ))

    assert result["status"] == "stale"
    stored = get_resume(resume_db, "u1", resume_id)
    assert stored["binding"] == "family" and stored["application_id"] is None
    assert stored["content_text"] == "A 旧内容"


def test_api_cas_loser_cleans_its_new_upload(tmp_path, monkeypatch):
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
        "主简历",
        "旧内容",
        family="backend",
        file_path=str(old_file),
        lines=[{"text": "旧内容", "knowledge_points": ["旧"]}],
    )
    assert resume_id is not None

    class ArchivingLLM(ScriptedLLM):
        def __init__(self):
            super().__init__([json.dumps(_valid_parse(), ensure_ascii=False)])

        async def chat(self, messages, **kwargs):
            assert archive_resume(settings.db_path, user_id, resume_id)
            return await super().chat(messages, **kwargs)

    monkeypatch.setattr(
        "careerdesk.features.resumes.api.build_llm",
        lambda _model, **_kwargs: ArchivingLLM(),
    )

    from careerdesk.bootstrap.app import create_app

    with TestClient(create_app()) as client:
        body = client.put(
            f"/api/resumes/{resume_id}",
            files={"file": ("late.md", b"late update", "text/markdown")},
        ).json()

    assert body["status"] == "processing"
    with TestClient(create_app()) as client:
        job = client.get("/api/resumes/jobs").json()["items"][0]
    assert job["state"] == "failed" and "更新" in job["message"]
    assert set(root.iterdir()) == {old_file}
    assert get_resume(settings.db_path, user_id, resume_id)["archived"] is True
    get_settings.cache_clear()


def test_all_supported_file_uploads_return_and_store_every_visible_segment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_LLM_MODEL", "deepseek:deepseek-chat")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.db_path)

    visible_lines = [f"完整可见段落 {index:03d}" for index in range(120)]
    extracted_text = "\n".join(visible_lines)
    output = {
        "family": "backend",
        "lines": [
            {"line_index": 0, "knowledge_points": ["开头"]},
            {"line_index": 119, "knowledge_points": ["结尾"]},
        ],
    }

    from careerdesk.features.resumes import api as resumes_api

    monkeypatch.setattr(
        resumes_api,
        "extract_document_text",
        lambda _path: extracted_text,
    )
    monkeypatch.setattr(
        resumes_api,
        "build_llm",
        lambda _model, **_kwargs: scripted(output),
    )

    from careerdesk.bootstrap.app import create_app

    suffixes = ("pdf", "docx", "md", "txt")
    with TestClient(create_app()) as client:
        for suffix in suffixes:
            response = client.post(
                "/api/resumes/upload",
                files={"file": (f"完整-{suffix}.{suffix}", b"local document", "application/octet-stream")},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "processing"

        jobs = client.get("/api/resumes/jobs").json()["items"]
        items = client.get("/api/resumes").json()["items"]

    assert len(jobs) == len(suffixes)
    assert all(job["state"] == "completed" for job in jobs)
    assert len(items) == len(suffixes)
    for item in items:
        stored = get_resume(settings.db_path, settings.dev_fake_user, item["id"])
        assert stored["content_text"] == extracted_text
        assert [line["text"] for line in stored["lines"]] == visible_lines
        assert stored["lines"][0]["knowledge_points"] == ["开头"]
        assert stored["lines"][-1]["knowledge_points"] == ["结尾"]
        assert all(
            line["knowledge_points"] == []
            for line in stored["lines"][1:-1]
        )
    get_settings.cache_clear()
