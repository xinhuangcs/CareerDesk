
import asyncio
import json

import pytest
from tests.support import ScriptedLLM
from pydantic import ValidationError

from careerdesk.features.research import ai_tasks
from careerdesk.features.research.ai_models import (
    MAX_PLAN_QUERIES,
    MAX_RESEARCH_CONFLICTS,
    MAX_RESEARCH_SECTION_CHARS,
    MAX_TECH_QUESTIONS,
    CompanyReport,
    PositionReport,
    ResearchPlan,
)
from careerdesk.platform.ai.structured_tasks import (
    INSUFFICIENT_CONTEXT,
    StructuredTaskCapacityError,
)


def run(coroutine):
    return asyncio.run(coroutine)


class CapturingScriptedLLM(ScriptedLLM):
    def __init__(self, responses: list[str]):
        super().__init__(responses, context_window=1_000_000)
        self.requests: list[dict] = []

    async def chat(self, messages, *, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return await super().chat(messages, tools=tools, **kwargs)


_ANCHOR = {
    "official_name": "示例公司", "website_domain": "example.com",
    "industry": "企业软件", "location": "上海", "confidence": "high", "note": "",
}


def _valid_company_report() -> dict:
    return {
        "business": {"text": "主营企业软件。", "sources": [1]},
        "culture": {"text": "重视客户价值。", "sources": [1, 2]},
        "recent_news": {"text": "未找到公开信息", "sources": []},
        "interview_style": {"text": "两轮技术面。", "sources": [2]},
        "source_conflicts": [],
    }


def _valid_position_report() -> dict:
    return {
        "key_takeaways": ["先复习分布式事务"],
        "interview_process": {"text": "三轮：两技术一 HR。", "sources": [1]},
        "experience_highlights": {"text": "常考手撕与项目追问。", "sources": [1]},
        "team_and_work_context": {"text": "未找到公开信息", "sources": []},
        "reported_questions": [{
            "text": "手写 LRU 缓存", "category": "professional_domain",
            "provenance": "reported", "sources": [1],
        }],
        "likely_questions": [{
            "text": "为什么选择我们？", "category": "hr_motivation",
            "provenance": "inference", "sources": [],
        }],
        "assessment_focuses": [{
            "text": "说明分布式事务取舍", "category": "professional_domain",
            "provenance": "inference", "sources": [1],
        }],
        "source_conflicts": [],
    }


def _materials() -> list[dict]:
    return [
        {"source_index": 1, "url": "https://example.com/a", "site": "example.com",
         "title": "官网", "date": "2026-05", "content": "官网介绍材料"},
        {"source_index": 2, "url": "https://nowcoder.com/1", "site": "nowcoder.com",
         "title": "面经", "date": "2026-06", "content": "面经材料"},
    ]


def test_company_report_guards_materials_and_retries_without_echoing_bad_output():
    canary = "BAD_OUTPUT_MUST_NOT_ENTER_RETRY_CONTEXT"
    llm = CapturingScriptedLLM([
        json.dumps({**_valid_company_report(), "unexpected": canary}, ensure_ascii=False),
        json.dumps(_valid_company_report(), ensure_ascii=False),
    ])

    result = run(ai_tasks.compose_company_report(
        llm, company="示例公司", anchor=_ANCHOR, materials=_materials(),
    ))

    assert result.business.text == "主营企业软件。"
    assert result.business.sources == [1]
    assert len(llm.requests) == 2
    assert canary not in json.dumps(llm.requests[1]["messages"], ensure_ascii=False)
    payload = next(
        str(message["content"])
        for message in llm.requests[0]["messages"]
        if message["role"] == "user"
    )
    assert "careerdesk_untrusted_company_report_input_v1" in payload
    assert "ignore them entirely" in payload
    assert payload.index('"anchor"') < payload.index("ignore them entirely")
    assert "官网介绍材料" in payload


def test_position_report_and_plan_use_distinct_untrusted_envelopes():
    plan_payload = {
        "anchor": _ANCHOR,
        "queries": [
            {"text": "示例公司 后端 面经", "leg": "position",
             "section": "experience_highlights", "kind": "general", "key": True},
        ],
    }
    llm = CapturingScriptedLLM([
        json.dumps(plan_payload, ensure_ascii=False),
        json.dumps(_valid_position_report(), ensure_ascii=False),
    ])

    plan = run(ai_tasks.compose_research_plan(
        llm, company="示例公司", position="后端开发", jd_excerpt="负责后端",
        department="基础架构", profile={"aliases": [], "notes": ""},
        presearch_materials=[{"title": "官网", "url": "https://example.com",
                              "snippet": "示例公司官网"}],
    ))
    report = run(ai_tasks.compose_position_report(
        llm, company="示例公司", position="后端开发", anchor=_ANCHOR,
        materials=_materials(),
    ))

    assert plan.anchor.website_domain == "example.com"
    assert plan.queries[0].key is True
    assert report.reported_questions[0].text == "手写 LRU 缓存"
    first_payload = next(str(m["content"]) for m in llm.requests[0]["messages"]
                         if m["role"] == "user")
    second_payload = next(str(m["content"]) for m in llm.requests[1]["messages"]
                          if m["role"] == "user")
    assert "careerdesk_untrusted_research_plan_input_v1" in first_payload
    assert "careerdesk_untrusted_position_report_input_v1" in second_payload


def test_research_capacity_failure_is_classified_without_swallowing_unknowns(monkeypatch):
    async def fail_capacity(*args, **kwargs):
        raise StructuredTaskCapacityError(INSUFFICIENT_CONTEXT)

    monkeypatch.setattr(ai_tasks, "run_structured_task", fail_capacity)
    with pytest.raises(ai_tasks.ResearchAITaskError, match="上下文容量不足"):
        run(ai_tasks.compose_company_report(
            object(), company="示例公司", anchor=_ANCHOR, materials=[],
        ))

    class UnknownFailure(RuntimeError):
        pass

    async def fail_unknown(*args, **kwargs):
        raise UnknownFailure("connection closed")

    monkeypatch.setattr(ai_tasks, "run_structured_task", fail_unknown)
    with pytest.raises(UnknownFailure, match="connection closed"):
        run(ai_tasks.compose_position_report(
            object(), company="示例公司", position="后端", anchor=_ANCHOR, materials=[],
        ))


def test_report_schemas_are_closed_required_bounded_and_deduplicated():
    valid = _valid_company_report()
    with pytest.raises(ValidationError, match="Field required"):
        CompanyReport.model_validate({
            key: value for key, value in valid.items() if key != "recent_news"
        })
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CompanyReport.model_validate({**valid, "unexpected": True})
    with pytest.raises(ValidationError, match="too_long"):
        CompanyReport.model_validate({
            **valid,
            "business": {"text": "x" * (MAX_RESEARCH_SECTION_CHARS + 1), "sources": []},
        })
    with pytest.raises(ValidationError, match="source 索引必须从 1 开始"):
        CompanyReport.model_validate({
            **valid,
            "business": {"text": "正文", "sources": [0]},
        })
    deduplicated = CompanyReport.model_validate({
        **valid,
        "culture": {"text": "文化", "sources": [2, 1, 2]},
    })
    assert deduplicated.culture.sources == [2, 1]
    conflicts = CompanyReport.model_validate({
        **valid,
        "source_conflicts": [
            {"summary": "官网与面经对远程政策的描述不一致。", "sources": [1, 2, 1]},
        ],
    })
    assert conflicts.source_conflicts[0].sources == [1, 2]
    with pytest.raises(ValidationError, match="至少两个不同来源"):
        CompanyReport.model_validate({
            **valid,
            "source_conflicts": [{"summary": "只有一个来源。", "sources": [1, 1]}],
        })
    with pytest.raises(ValidationError, match="too_long"):
        CompanyReport.model_validate({
            **valid,
            "source_conflicts": [
                {"summary": f"冲突 {index}", "sources": [1, 2]}
                for index in range(MAX_RESEARCH_CONFLICTS + 1)
            ],
        })

    position = _valid_position_report()
    merged = PositionReport.model_validate({
        **position,
        "reported_questions": [
            {"text": "手写 LRU 缓存", "category": "professional_domain",
             "provenance": "reported", "sources": [1]},
            {"text": "手写 lru 缓存", "category": "professional_domain",
             "provenance": "reported", "sources": [2]},
            {"text": "TCP 三次握手", "category": "professional_domain",
             "provenance": "reported", "sources": [1]},
        ],
    })
    assert [item.text for item in merged.reported_questions] == ["手写 LRU 缓存", "TCP 三次握手"]
    with pytest.raises(ValidationError, match="too_long"):
        PositionReport.model_validate({
            **position,
            "reported_questions": [
                {"text": f"题目 {index}", "category": "professional_domain",
                 "provenance": "reported", "sources": [1]}
                for index in range(MAX_TECH_QUESTIONS + 1)
            ],
        })


def test_plan_schema_bounds_queries_and_locks_enums():
    base_query = {"text": "示例公司 面经", "leg": "position",
                  "section": "experience_highlights"}
    with pytest.raises(ValidationError, match="too_long"):
        ResearchPlan.model_validate({
            "anchor": _ANCHOR,
            "queries": [
                {**base_query, "text": f"查询 {index}"}
                for index in range(MAX_PLAN_QUERIES + 1)
            ],
        })
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate({
            "anchor": {**_ANCHOR, "confidence": "medium"},
            "queries": [],
        })
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate({
            "anchor": _ANCHOR,
            "queries": [{**base_query, "leg": "unknown"}],
        })
    plan = ResearchPlan.model_validate({"anchor": _ANCHOR, "queries": [base_query]})
    assert plan.queries[0].kind == "general" and plan.queries[0].key is False
