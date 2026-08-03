
import asyncio
import gc
import json
from datetime import datetime

import pytest
from tests.support import ScriptedLLM

from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.features.research.public import (
    ResearchService,
    get_research_cache,
    research_is_fresh,
)
from careerdesk.features.research import service as research_module
from careerdesk.features.research.providers import QueryOutcome, SearchHit
from careerdesk.features.research.repository import save_research_cache
from careerdesk.core.config import get_settings
from careerdesk.orchestration.application_prep.briefing import compose_briefing
from careerdesk.orchestration.application_prep.service import PrepService
from careerdesk.features.reviews.public import ReviewService
from tests.review_record_test_helpers import execute_review_record

TODAY = "2026-07-07"

ANCHOR = {
    "official_name": "米哈游", "website_domain": "mihoyo.com",
    "industry": "游戏", "location": "上海", "confidence": "high", "note": "",
}


def plan_payload(company: str = "米哈游", position: str = "AI工程师") -> dict:
    return {
        "anchor": ANCHOR,
        "queries": [
            {"text": f"{company} 业务", "leg": "company", "section": "business", "key": True},
            {"text": f"{company} 文化", "leg": "company", "section": "culture"},
            {"text": f"{company} 新闻", "leg": "company", "section": "recent_news",
             "kind": "news"},
            {"text": f"{company} 面试", "leg": "company", "section": "interview_style"},
            {"text": f"{company} {position} 面经", "leg": "position",
             "section": "experience_highlights", "key": True},
            {"text": f"{company} {position} 流程", "leg": "position",
             "section": "interview_process"},
        ],
    }


COMPANY_REPORT_FULL = {
    "business": {"text": "做二次元游戏，主力产品是开放世界手游。", "sources": [1]},
    "culture": {"text": "技术宅拯救世界，重产品打磨。", "sources": [1]},
    "recent_news": {"text": "刚完成新一轮组织扩张，AI 团队在扩招。", "sources": [1]},
    "interview_style": {"text": "三轮技术面+HR 面，爱手撕 attention。", "sources": [1]},
    "source_conflicts": [],
}
COMPANY_REPORT_MISSING_NEWS = {
    **COMPANY_REPORT_FULL,
    "recent_news": {"text": "未找到公开信息", "sources": []},
}
POSITION_REPORT_FULL = {
    "key_takeaways": ["先复习 attention 手撕", "准备开放世界项目故事"],
    "interview_process": {"text": "三轮：两轮技术 + 一轮 HR。", "sources": [1]},
    "experience_highlights": {"text": "面经反复出现 attention 手撕。", "sources": [1]},
    "team_and_work_context": {"text": "AI 平台组用 PyTorch。", "sources": [1]},
    "reported_questions": [{
        "text": "手撕 attention", "category": "professional_domain",
        "provenance": "reported", "sources": [1],
    }],
    "likely_questions": [{
        "text": "为什么想来我们公司？", "category": "hr_motivation",
        "provenance": "inference", "sources": [],
    }],
    "assessment_focuses": [{
        "text": "讲清项目取舍", "category": "resume_deep_dive",
        "provenance": "inference", "sources": [1],
    }],
    "source_conflicts": [],
}


class FakePool:

    has_outlets = True

    def __init__(self, *, news_first_round_empty: bool = True, fail_all: bool = False):
        self.queries: list = []
        self._news_empty = news_first_round_empty
        self._fail_all = fail_all

    async def run_plan(self, queries):
        await asyncio.sleep(0)
        outcomes = []
        for query in queries:
            self.queries.append(query)
            if self._fail_all:
                outcomes.append(QueryOutcome(query=query, failed_engines=["Tavily"]))
                continue
            gap_round = "融资 裁员" in query.text
            if (query.section == "recent_news" and self._news_empty and not gap_round):
                outcomes.append(QueryOutcome(query=query))
                continue
            hit = SearchHit(
                title=f"{query.section} 材料",
                url=f"https://material.example/{len(self.queries)}",
                snippet=f"关于「{query.text}」的搜索材料若干。",
                engine="Tavily",
                raw_content=f"关于「{query.text}」的正文材料。",
            )
            outcomes.append(QueryOutcome(query=query, hits=[hit]))
        return outcomes


class FakeFetcher:

    def __init__(self):
        self.requested: list[list[str]] = []

    async def fetch_pages(self, urls):
        self.requested.append(list(urls))
        return {}


def scripted(*payloads) -> ScriptedLLM:
    return ScriptedLLM([json.dumps(payload, ensure_ascii=False) for payload in payloads])


def run(coroutine):
    return asyncio.run(coroutine)


def build_service(db_path: str, llm, pool) -> ResearchService:
    return ResearchService(db_path, llm, pool, fetcher=FakeFetcher())


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "biz.db")
    init_db(path)
    return path


def seed_application(db_path: str, company: str = "米哈游", position: str = "AI工程师",
                     next_action_date: str | None = None) -> int:
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO applications (user_id, company, position, next_stage, next_step, "
            "next_date, created_time, updated_time) VALUES ('u1', ?, ?, ?, ?, ?, ?, ?)",
            (
                company,
                position,
                "applied" if next_action_date else None,
                "跟进" if next_action_date else None,
                next_action_date,
                now_iso(),
                now_iso(),
            ),
        )
        return cursor.lastrowid


def test_research_gap_round_then_company_cache_reuse(db_path):
    pool = FakePool()
    llm = scripted(plan_payload(), COMPANY_REPORT_MISSING_NEWS, POSITION_REPORT_FULL,
                   COMPANY_REPORT_FULL)
    service = build_service(db_path, llm, pool)

    result = run(service.research("u1", "米哈游", "AI工程师", today=TODAY))
    assert result["status"] == "ok" and result["company_from_cache"] is False
    assert result["company_report"]["recent_news"]["text"].startswith("刚完成")
    assert result["company_report"]["version"] == 3
    assert result["company_report"]["anchor"]["website_domain"] == "mihoyo.com"
    assert result["position_report"]["key_takeaways"]
    assert len(pool.queries) == 8 and llm.calls == 4

    cached = get_research_cache(db_path, "u1", "米哈游")
    assert cached["research"]["version"] == 3

    second_llm = scripted(plan_payload(position="平台工程师"), POSITION_REPORT_FULL)
    second_pool = FakePool(news_first_round_empty=False)
    second = run(build_service(db_path, second_llm, second_pool).research(
        "u1", "米哈游", "平台工程师", today=TODAY))
    assert second["status"] == "ok" and second["company_from_cache"] is True
    assert len(second_pool.queries) == 3 and second_llm.calls == 2


def test_plan_failure_degrades_to_skeleton_queries(db_path):
    pool = FakePool(news_first_round_empty=False)
    llm = ScriptedLLM([
        "这不是 JSON 计划",
        "还不是 JSON",
        json.dumps(COMPANY_REPORT_FULL, ensure_ascii=False),
        json.dumps(POSITION_REPORT_FULL, ensure_ascii=False),
    ])
    result = run(build_service(db_path, llm, pool).research(
        "u1", "米哈游", "AI工程师", today=TODAY))

    assert result["status"] == "ok" and result["planner"] == "skeleton"
    assert result["anchor"]["confidence"] == "low"
    assert len(pool.queries) == 11


def test_refresh_regenerates_fresh_company_cache(db_path):
    refreshed = {
        **COMPANY_REPORT_FULL,
        "recent_news": {"text": "刚发布新的 AI 产品。", "sources": [1]},
    }
    first_llm = scripted(plan_payload(), COMPANY_REPORT_FULL, POSITION_REPORT_FULL)
    assert run(build_service(db_path, first_llm, FakePool(news_first_round_empty=False))
               .research("u1", "米哈游", "AI工程师", today=TODAY))["status"] == "ok"

    refresh_llm = scripted(plan_payload(), refreshed, POSITION_REPORT_FULL)
    result = run(build_service(db_path, refresh_llm, FakePool(news_first_round_empty=False))
                 .research("u1", "米哈游", "AI工程师", today=TODAY, refresh=True))

    assert result["status"] == "ok" and result["company_from_cache"] is False
    assert result["company_report"]["recent_news"]["text"] == "刚发布新的 AI 产品。"
    assert get_research_cache(db_path, "u1", "米哈游")["research"]["recent_news"][
        "text"] == "刚发布新的 AI 产品。"


def test_research_singleflight_coalesces_concurrent_company_cache_misses(db_path):
    pool = FakePool(news_first_round_empty=False)
    llm = scripted(
        plan_payload(), COMPANY_REPORT_FULL, POSITION_REPORT_FULL,
        plan_payload(position="平台工程师"), POSITION_REPORT_FULL,
    )
    first = build_service(db_path, llm, pool)
    second = build_service(db_path, llm, pool)

    async def run_both():
        return await asyncio.gather(
            first.research("u1", "米哈游", "AI工程师", today=TODAY),
            second.research("u1", "米哈游", "平台工程师", today=TODAY),
        )

    results = run(run_both())

    assert [item["status"] for item in results] == ["ok", "ok"]
    assert sorted(item["company_from_cache"] for item in results) == [False, True]
    assert llm.calls == 5


def test_research_singleflight_coalesces_concurrent_refreshes(db_path):
    refreshed = {
        **COMPANY_REPORT_FULL,
        "recent_news": {"text": "新的公司动态。", "sources": [1]},
    }
    seed_llm = scripted(plan_payload(), COMPANY_REPORT_FULL, POSITION_REPORT_FULL)
    assert run(build_service(db_path, seed_llm, FakePool(news_first_round_empty=False))
               .research("u1", "米哈游", "AI工程师", today=TODAY))["status"] == "ok"

    llm = scripted(
        plan_payload(), refreshed, POSITION_REPORT_FULL,
        plan_payload(position="平台工程师"), POSITION_REPORT_FULL,
    )
    pool = FakePool(news_first_round_empty=False)
    first = build_service(db_path, llm, pool)
    second = build_service(db_path, llm, pool)

    async def refresh_both():
        return await asyncio.gather(
            first.research("u1", "米哈游", "AI工程师", today=TODAY, refresh=True),
            second.research("u1", "米哈游", "平台工程师", today=TODAY, refresh=True),
        )

    results = run(refresh_both())

    assert [item["status"] for item in results] == ["ok", "ok"]
    assert sorted(item["company_from_cache"] for item in results) == [False, True]
    assert get_research_cache(db_path, "u1", "米哈游")["research"]["recent_news"][
        "text"] == "新的公司动态。"
    assert llm.calls == 5


def test_research_singleflight_does_not_retain_closed_event_loops(db_path):
    async def contend(index: int):
        lock = research_module._research_lock(db_path, "u1", f"公司-{index}")
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        lock.release()
        await waiter
        lock.release()

    for index in range(10):
        run(contend(index))
    gc.collect()

    assert len(research_module._RESEARCH_LOCKS) == 0


def test_research_freshness_window(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv("APP_TIMEZONE", "Asia/Shanghai")
        get_settings.cache_clear()
        assert research_is_fresh("2026-07-01T08:00:00+00:00", TODAY) is True
        assert research_is_fresh("2026-06-01T08:00:00+00:00", TODAY) is False
        assert research_is_fresh(None, TODAY) is False
        # Project UTC storage into the explicit application timezone. The replay date
        # comes from today, while time-of-day crosses the exact TTL boundary normally.
        assert research_is_fresh(
            "2026-06-22T20:00:00+00:00",
            TODAY,
            now=datetime.fromisoformat("2026-07-06T19:59:59+00:00"),
        ) is True
        assert research_is_fresh(
            "2026-06-22T20:00:00+00:00",
            TODAY,
            now=datetime.fromisoformat("2026-07-06T20:00:01+00:00"),
        ) is False
    get_settings.cache_clear()


def test_disabled_web_research_never_searches_or_composes_and_can_read_cache(db_path):
    llm = ScriptedLLM([])
    service = ResearchService(db_path, llm, None)

    absent = run(service.research("u1", "米哈游", "AI工程师", today=TODAY))
    assert absent["status"] == "disabled" and absent["company_report"] is None
    assert absent["position_report"] is None and llm.calls == 0

    cached_report = {"version": 2, "anchor": ANCHOR, **COMPANY_REPORT_FULL,
                     "sources": [], "planner": "model"}
    save_research_cache(db_path, "u1", "米哈游", cached_report)
    cached = run(service.research("u1", "米哈游", "AI工程师", today=TODAY, refresh=True))
    assert cached["status"] == "disabled" and cached["company_report"] == cached_report
    assert llm.calls == 0


def test_company_research_cache_keeps_both_output_locales(db_path):
    chinese = {"version": 3, "business": {"text": "中文报告", "sources": []}}
    english = {"version": 3, "business": {"text": "English report", "sources": []}}
    profile = {"version": 1, "primary_language": "zh", "secondary_language": "en", "country": None}

    save_research_cache(
        db_path,
        "u1",
        "双语公司",
        chinese,
        output_locale="zh-CN",
        search_profile=profile,
    )
    save_research_cache(
        db_path,
        "u1",
        "双语公司",
        english,
        output_locale="en",
        search_profile=profile,
    )

    assert get_research_cache(
        db_path, "u1", "双语公司", output_locale="zh-CN"
    )["research"] == chinese
    assert get_research_cache(
        db_path, "u1", "双语公司", output_locale="en"
    )["research"] == english


def test_all_outlets_failing_degrades_without_composing_or_caching(db_path):
    llm = scripted(plan_payload())
    result = run(build_service(db_path, llm, FakePool(fail_all=True)).research(
        "u1", "示例公司", "后端", today=TODAY))

    assert result["status"] == "unavailable" and result["company_report"] is None
    assert llm.calls == 1
    assert get_research_cache(db_path, "u1", "示例公司") is None

    stale_report = {"version": 2, "anchor": ANCHOR, **COMPANY_REPORT_FULL,
                    "sources": [], "planner": "model"}
    save_research_cache(db_path, "u1", "示例公司", stale_report)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE companies SET research_time='2026-06-01T00:00:00+00:00' "
            "WHERE user_id='u1' AND name='示例公司'",
        )
    reused = run(build_service(db_path, scripted(plan_payload()), FakePool(fail_all=True))
                 .research("u1", "示例公司", "后端", today=TODAY))
    assert reused["status"] == "unavailable"
    assert reused["company_report"] == stale_report


def test_review_derive_does_not_touch_prep(db_path):
    extraction = {
        "company": "字节",
        "position": "LLM应用",
        "history": {
            "step": "二面",
            "date": TODAY,
            "outcome": "passed",
            "summary": "二面完成",
        },
        "projected_state": {"stage": "interviewing", "current_step": "二面"},
        "next_action": None,
        "questions": [],
        "mood": None,
        "time_of_day": None,
        "factors": [],
    }
    service = ReviewService(db_path, scripted(extraction))
    run(execute_review_record(service, "u1", "面了字节二面", today=TODAY))
    with read_connection(db_path) as conn:
        (prep_status,) = conn.execute("SELECT prep_status FROM applications").fetchone()
    assert prep_status == "none"


def test_prep_service_runs_research_and_marks_ready_without_mutating_library(db_path):
    application_id = seed_application(db_path)
    with read_connection(db_path) as conn:
        question_count_before = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    llm = scripted(plan_payload(), COMPANY_REPORT_FULL, POSITION_REPORT_FULL)
    service = PrepService(
        db_path,
        build_service(db_path, llm, FakePool(news_first_round_empty=False)),
    )

    result = run(service.run("u1", application_id, today=TODAY))
    assert result["status"] == "ok"
    prep = result["prep"]
    assert prep["research"] == "ok"
    assert prep["position_report"]["key_takeaways"]
    assert prep["anchor"]["official_name"] == "米哈游"

    with read_connection(db_path) as conn:
        question_count_after = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        prep_status, prep_json = conn.execute(
            "SELECT prep_status, prep_json FROM applications WHERE id=?", (application_id,)
        ).fetchone()
    assert question_count_after == question_count_before
    assert prep_status == "ready"
    assert json.loads(prep_json)["localized"]["zh-CN"]["research"] == "ok"

    assert run(service.run("u1", 99999))["status"] == "error"


def test_prep_service_keeps_running_when_search_outlets_are_unavailable(db_path):
    application_id = seed_application(db_path, company="示例公司")
    service = PrepService(
        db_path,
        build_service(db_path, scripted(plan_payload()), FakePool(fail_all=True)),
    )

    result = run(service.run("u1", application_id, today=TODAY))

    assert result["status"] == "ok"
    assert result["prep"]["research"] == "unavailable"
    briefing = compose_briefing(db_path, "u1", application_id, today=TODAY)
    assert "搜索出口暂时不可用" in briefing["markdown"]


def test_prep_completion_preserves_concurrent_artifact_written_during_run(db_path):
    application_id = seed_application(db_path)

    class ConcurrentArtifactResearch:
        async def research(self, user_id, company, position, **kwargs):
            with transaction(db_path) as conn:
                conn.execute(
                    "UPDATE applications SET prep_json=? WHERE id=?",
                    (json.dumps({"unrelated_artifact": {"name": "A 版"}},
                                ensure_ascii=False), application_id),
                )
            return {"status": "ok", "company_report": None, "company_from_cache": False,
                    "position_report": None, "anchor": None, "planner": "skeleton",
                    "web_question_candidates": []}

    result = run(PrepService(db_path, ConcurrentArtifactResearch()).run(
        "u1", application_id, today=TODAY))

    assert result["status"] == "ok"
    assert result["prep"]["unrelated_artifact"] == {"name": "A 版"}


def test_briefing_is_pure_code_with_new_layout_and_sources(db_path):
    application_id = seed_application(db_path, company="字节", position="LLM应用")
    seed_application(db_path, company="B厂", position="岗2", next_action_date="2026-07-08")
    company_report = {
        "version": 3,
        "anchor": {**ANCHOR, "official_name": "字节跳动", "website_domain": "bytedance.com"},
        **COMPANY_REPORT_FULL,
        "sources": [{"index": 1, "url": "https://bytedance.com/about", "site": "bytedance.com",
                     "title": "官网", "date": "2026-06", "engines": ["Tavily"]}],
        "planner": "model",
    }
    save_research_cache(db_path, "u1", "字节", company_report)
    with transaction(db_path) as conn:
        conn.execute("UPDATE companies SET research_time='2026-06-01T00:00:00+00:00' WHERE name='字节'")
    position_report = {
        **POSITION_REPORT_FULL,
        "sources": [{"index": 1, "url": "https://nowcoder.com/1", "site": "nowcoder.com",
                     "title": "面经", "date": "2026-06", "engines": ["Brave"]}],
    }
    prep = {
        "research": "ok", "prepared_time": f"{TODAY}T08:00:00+00:00",
        "anchor": company_report["anchor"],
        "position_report": position_report,
        "web_questions": {"imported": 1, "skipped_duplicates": 0},
        "nontech_answers": [{"question": "为什么想来我们公司？", "answer": "因为热爱。"}],
    }
    with transaction(db_path) as conn:
        conn.execute("UPDATE applications SET prep_status='ready', prep_json=? WHERE id=?",
                     (json.dumps(prep, ensure_ascii=False), application_id))

    result = compose_briefing(db_path, "u1", application_id, today=TODAY)
    assert result["status"] == "ok"
    markdown = result["markdown"]
    assert "公司调研：字节 · LLM应用" in markdown
    assert "超过 14 天" in markdown
    assert "先复习 attention 手撕" in markdown
    assert "技术宅拯救世界" in markdown
    assert "面经反复出现 attention 手撕" in markdown
    assert "参考来源" in markdown and "nowcoder.com" in markdown
    assert "本调研基于：字节跳动、bytedance.com" in markdown
    assert markdown.index("## 公司速览") < markdown.index("## 这个岗位怎么面")
    assert markdown.index("## 考前必读") < markdown.index("## 公司速览")
    for gone in ("前轮回顾", "这家问过你的真题", "你当前最弱的点", "未来 3 天", "状态提醒"):
        assert gone not in markdown

    assert compose_briefing(db_path, "u1", 99999)["status"] == "error"


def test_briefing_renders_legacy_v1_cache_with_upgrade_hint(db_path):
    application_id = seed_application(db_path, company="老缓存公司")
    save_research_cache(db_path, "u1", "老缓存公司", {
        "business": "老业务描述。", "culture": "老文化。", "recent_news": "老新闻。",
        "interview_style": "老风格。", "likely_questions": ["为什么选我们？"],
    })

    markdown = compose_briefing(db_path, "u1", application_id, today=TODAY)["markdown"]

    assert "旧版调研（无来源标注）" in markdown
    assert "老业务描述。" in markdown
