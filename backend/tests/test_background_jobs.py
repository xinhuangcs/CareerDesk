
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest

from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.applications import repository as timeline
from careerdesk.features.applications.intake_models import ParsedPosition
from careerdesk.features.applications.public import execute_application_update_operation
from careerdesk.orchestration.application_prep.service import PrepService
from careerdesk.features.research.public import ResearchService, get_research_cache
from careerdesk.features.research import repository as research_repository


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    return path


def _application(db_path: str, user_id: str = "u1") -> int:
    with transaction(db_path) as conn:
        return conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES (?, '示例公司', '后端工程师', ?, ?)",
            (user_id, now_iso(), now_iso()),
        ).lastrowid


def _claim_prep(db_path: str, user_id: str, application_id: int, generation: str,
                **kwargs) -> str:
    return timeline.claim_prep_generation(
        db_path, user_id, application_id, generation, **kwargs)["status"]


def _update_application(db_path: str, *, company: str = "示例公司",
                        position: str = "后端工程师", changes: dict) -> dict:
    return execute_application_update_operation(
        db_path,
        "u1",
        operation_id=uuid4(),
        client_turn_id=uuid4(),
        company=company,
        position=position,
        changes=changes,
    )


def test_prep_claim_reuses_running_and_force_invalidates_old_result(db_path):
    application_id = _application(db_path)
    started = timeline.claim_prep_generation(db_path, "u1", application_id, "old")
    assert started["status"] == "started" and started["takeover"] is False
    assert started["retry_after_seconds"] == timeline.PREP_JOB_LEASE_SECONDS
    running = timeline.claim_prep_generation(db_path, "u1", application_id, "duplicate")
    assert running["status"] == "running" and running["retry_after_seconds"] > 0
    assert timeline.set_prep_status(
        db_path, "u1", application_id, "running", generation="old")

    assert _claim_prep(
        db_path, "u1", application_id, "too-early", force=True) == "running"
    takeover = timeline.claim_prep_generation(
        db_path, "u1", application_id, "new", force=True, lease_seconds=0)
    assert takeover == {"status": "started", "takeover": True, "retry_after_seconds": 0}
    assert not timeline.fail_prep_generation(
        db_path, "u1", application_id, "old worker failed", generation="old")
    assert not timeline.set_prep_status(
        db_path, "u1", application_id, "ready", prep={"winner": "old"}, generation="old")
    assert timeline.set_prep_status(
        db_path, "u1", application_id, "ready", prep={"winner": "new"}, generation="new")
    detail = timeline.application_detail(db_path, "u1", application_id)
    assert detail["prep_status"] == "ready" and detail["prep"] == {"winner": "new"}
    assert detail["prep_retry_after_seconds"] is None


def test_ready_prep_is_reused_unless_user_explicitly_requests_refresh(db_path):
    application_id = _application(db_path)
    assert _claim_prep(db_path, "u1", application_id, "first") == "started"
    assert timeline.set_prep_status(
        db_path, "u1", application_id, "ready", prep={"prepared_time": "now"},
        generation="first")

    reused = timeline.claim_prep_generation(db_path, "u1", application_id, "stale-page")
    assert reused["status"] == "completed" and reused["prep_status"] == "ready"

    restarted = timeline.claim_prep_generation(
        db_path, "u1", application_id, "explicit-refresh", restart_ready=True)
    assert restarted["status"] == "started" and restarted["takeover"] is False


def test_prep_failure_preserves_last_successful_artifacts(db_path):
    application_id = _application(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_status = 'ready', prep_json = ? WHERE id = ?",
            ('{"unrelated_artifact":{"version":"last-good"},"prepared_time":"old"}', application_id),
        )
    assert _claim_prep(
        db_path, "u1", application_id, "refresh", restart_ready=True) == "started"

    assert timeline.fail_prep_generation(
        db_path, "u1", application_id, "网络暂时不可用", generation="refresh")

    detail = timeline.application_detail(db_path, "u1", application_id)
    assert detail["prep_status"] == "failed"
    assert detail["prep"]["unrelated_artifact"] == {"version": "last-good"}
    assert detail["prep"]["prepared_time"] == "old"
    assert detail["prep"]["error"] == "网络暂时不可用"


def test_prep_force_takeover_has_exactly_one_winner(db_path):
    application_id = _application(db_path)
    assert _claim_prep(db_path, "u1", application_id, "expired") == "started"
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_heartbeat_time = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (application_id,),
        )
    before_priority = timeline.application_detail(db_path, "u1", application_id)
    assert before_priority["prep_retry_after_seconds"] == 0
    assert timeline.set_priority(
        db_path,
        "u1",
        application_id,
        "high",
        expected_revision=0,
    )
    after_priority = timeline.application_detail(db_path, "u1", application_id)
    assert after_priority["prep_retry_after_seconds"] == 0
    assert after_priority["revision"] == 1
    barrier = Barrier(2)

    def claim(generation: str) -> dict:
        barrier.wait()
        return timeline.claim_prep_generation(
            db_path, "u1", application_id, generation, force=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["first", "second"]))

    assert sorted(item["status"] for item in claims) == ["running", "started"]
    assert sum(item["takeover"] is True for item in claims) == 1


def test_prep_service_cannot_finish_after_force_restart(db_path):
    application_id = _application(db_path)
    assert _claim_prep(db_path, "u1", application_id, "old") == "started"

    class SupersedingResearch:
        async def research(self, *args, **kwargs):
            assert _claim_prep(
                db_path, "u1", application_id, "new", force=True,
                lease_seconds=0) == "started"
            return {"status": "ok", "company_report": None, "company_from_cache": False,
                    "position_report": None, "anchor": None, "planner": None,
                    "web_question_candidates": []}

    result = asyncio.run(PrepService(db_path, SupersedingResearch()).run(
        "u1", application_id, generation="old"))
    assert result["status"] == "stale"
    with read_connection(db_path) as conn:
        status, generation = conn.execute(
            "SELECT prep_status, prep_generation FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
    assert (status, generation) == ("pending", "new")


@pytest.mark.parametrize("status", ["ok", "error", "stale"])
def test_owned_prep_llm_closes_for_every_job_result(
    db_path,
    monkeypatch,
    status,
):
    class OwnedLLM:
        def __init__(self):
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    llm = OwnedLLM()
    service = PrepService(db_path, object(), llm=llm)
    expected = {"status": status}

    async def finish(*_args, **_kwargs):
        return expected

    monkeypatch.setattr(service, "_run_once", finish)

    assert asyncio.run(service.run("u1", 1)) is expected
    assert llm.close_calls == 1


def test_stale_prep_cannot_write_company_research_cache(db_path):
    from careerdesk.features.research.providers import QueryOutcome

    application_id = _application(db_path)
    assert _claim_prep(db_path, "u1", application_id, "old") == "started"

    class TakeoverPool:
        has_outlets = True

        def __init__(self):
            self.calls = 0

        async def run_plan(self, queries):
            await asyncio.sleep(0)
            self.calls += 1
            if self.calls == 1:
                assert _claim_prep(
                    db_path, "u1", application_id, "new", force=True,
                    lease_seconds=0) == "started"
            return [QueryOutcome(query=query) for query in queries]

    class ForbiddenLLM:
        async def chat(self, *args, **kwargs):
            raise AssertionError("superseded task must not pay for the plan call")

    pool = TakeoverPool()
    service = ResearchService(db_path, ForbiddenLLM(), pool, fetcher=None)
    result = asyncio.run(PrepService(db_path, service).run(
        "u1", application_id, generation="old"))
    assert result["status"] == "stale"
    assert pool.calls == 1
    assert get_research_cache(db_path, "u1", "示例公司") is None


@pytest.mark.parametrize(
    "scope",
    [
        {"application_id": 1},
        {"generation": "generation-only"},
    ],
)
def test_research_generation_scope_must_be_complete_before_search(db_path, scope):
    service = ResearchService(db_path, object(), None)

    with pytest.raises(ValueError, match="必须同时提供"):
        asyncio.run(service.research("u1", "示例公司", "后端开发", **scope))

    assert get_research_cache(db_path, "u1", "示例公司") is None


@pytest.mark.parametrize(
    "scope",
    [
        {"application_id": 1},
        {"generation": "generation-only"},
    ],
)
def test_research_repository_rejects_partial_generation_scope(db_path, scope):
    with pytest.raises(ValueError, match="必须同时提供"):
        research_repository.save_research_cache(
            db_path, "u1", "示例公司", {"business": "材料"}, **scope)


def test_research_generation_cannot_write_another_company_cache(db_path):
    application_id = _application(db_path)
    assert _claim_prep(
        db_path, "u1", application_id, "owner") == "started"

    assert research_repository.save_research_cache(
        db_path,
        "u1",
        "另一家公司",
        {"business": "错误材料"},
        application_id=application_id,
        generation="owner",
    ) is None
    assert get_research_cache(db_path, "u1", "另一家公司") is None

    assert research_repository.save_research_cache(
        db_path,
        "u1",
        "示例公司",
        {"business": "正确材料"},
        application_id=application_id,
        generation="owner",
    ) is not None
    assert get_research_cache(db_path, "u1", "示例公司")["research"] == {
        "business": "正确材料"
    }


def test_research_generation_company_mismatch_fails_before_cache_or_search(db_path):
    application_id = _application(db_path)
    assert _claim_prep(
        db_path, "u1", application_id, "owner") == "started"
    research_repository.save_research_cache(
        db_path, "u1", "另一家公司", {"business": "现有缓存"})

    class ForbiddenPool:
        has_outlets = True

        async def run_plan(self, queries):
            raise AssertionError("mismatched token must fail before any search")

    service = ResearchService(db_path, object(), ForbiddenPool(), fetcher=None)

    result = asyncio.run(service.research(
        "u1",
        "另一家公司",
        "后端开发",
        application_id=application_id,
        generation="owner",
    ))

    assert result["status"] == "stale"


def test_prep_surfaces_research_task_error_message_verbatim(db_path):
    from careerdesk.features.research.public import ResearchAITaskError

    application_id = _application(db_path)

    class CapacityFailingResearch:
        async def research(self, *_args, **_kwargs):
            raise ResearchAITaskError("当前模型的上下文容量不足，无法安全生成公司调研报告；请换用上下文更大的模型后重试")

    result = asyncio.run(PrepService(db_path, CapacityFailingResearch()).run(
        "u1", application_id))

    assert result["status"] == "error" and "上下文容量不足" in result["message"]
    detail = timeline.application_detail(db_path, "u1", application_id)
    assert detail["prep_status"] == "failed"
    assert "上下文容量不足" in detail["prep"]["error"]


def test_prep_service_forwards_refresh_and_sanitizes_failure(db_path):
    application_id = _application(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            ('{"unrelated_artifact":{"version":"last-good"}}', application_id),
        )
    calls: list[dict] = []

    class FailingResearch:
        async def research(self, *_args, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("provider-secret-detail")

    result = asyncio.run(PrepService(db_path, FailingResearch()).run(
        "u1", application_id, refresh_research=True))

    assert result["status"] == "error" and "provider-secret-detail" not in result["message"]
    assert calls[0]["refresh"] is True
    detail = timeline.application_detail(db_path, "u1", application_id)
    assert detail["prep_status"] == "failed"
    assert detail["prep"]["unrelated_artifact"] == {"version": "last-good"}
    assert "provider-secret-detail" not in detail["prep"]["error"]


def test_research_save_serializes_generation_guard_with_rename(db_path, monkeypatch):
    application_id = _application(db_path)
    assert _claim_prep(
        db_path, "u1", application_id, "owner") == "started"
    entered_ensure = Event()
    release_save = Event()
    rename_finished = Event()
    original_ensure = research_repository.ensure_company_in_transaction

    def blocking_ensure(conn, user_id, company):
        entered_ensure.set()
        assert release_save.wait(timeout=5)
        return original_ensure(conn, user_id, company)

    monkeypatch.setattr(
        research_repository, "ensure_company_in_transaction", blocking_ensure)

    def save():
        return research_repository.save_research_cache(
            db_path,
            "u1",
            "示例公司",
            {"business": "有效材料"},
            application_id=application_id,
            generation="owner",
        )

    def rename():
        try:
            return _update_application(
                db_path,
                changes={"company": "新公司"},
            )
        finally:
            rename_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        save_future = pool.submit(save)
        assert entered_ensure.wait(timeout=5)
        rename_future = pool.submit(rename)
        assert not rename_finished.wait(timeout=0.2)
        release_save.set()
        assert save_future.result(timeout=5) is not None
        assert rename_future.result(timeout=5)["state"] == "completed"

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT prep_generation FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0] is None


def test_prep_inputs_invalidate_running_generation(db_path):
    application_id = _application(db_path)
    assert _claim_prep(db_path, "u1", application_id, "rename") == "started"
    renamed = _update_application(
        db_path,
        changes={"position": "平台工程师"},
    )
    assert renamed["state"] == "completed"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT prep_status, prep_generation FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("none", None)

    assert _claim_prep(db_path, "u1", application_id, "jd") == "started"
    positions = [{
        "company": "示例公司", "position": "平台工程师", "jd_text": "新的岗位要求",
        "jd_source_start": 0, "jd_source_end": len("新的岗位要求"),
        "skills": ["Python"], "highlights": [],
    }]
    journal_id, operation_id = timeline.create_intake_batch(db_path, "u1", "x")
    assert timeline.activate_intake_proposal(
        db_path, "u1", journal_id,
        [ParsedPosition.model_validate(position) for position in positions],
    )
    operation = timeline.approve_intake_operation(db_path, "u1", operation_id)
    assert operation["state"] == "completed"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT prep_status, prep_generation FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("none", None)


def test_prep_total_timeout_closes_generation(db_path, monkeypatch):
    from careerdesk.orchestration.application_prep import service as service_module

    application_id = _application(db_path)
    assert _claim_prep(db_path, "u1", application_id, "slow") == "started"

    class SlowResearch:
        async def research(self, *args, **kwargs):
            await asyncio.sleep(1)
            return {"status": "ok", "company_report": None, "company_from_cache": False,
                    "position_report": None, "anchor": None, "planner": None,
                    "web_question_candidates": []}

    monkeypatch.setattr(service_module, "PREP_JOB_TIMEOUT_SECONDS", 0.01)
    result = asyncio.run(PrepService(db_path, SlowResearch()).run(
        "u1", application_id, generation="slow"))
    assert result["status"] == "error" and "已安全停止" in result["message"]
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT prep_status, prep_generation FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone() == ("failed", None)
