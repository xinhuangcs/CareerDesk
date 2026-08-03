
import asyncio
import json

import pytest
from agentmaker.core.adapters import AnthropicAdapter, GeminiAdapter, OpenAIAdapter
from tests.support import ScriptedLLM

from careerdesk.orchestration.application_prep import ai_tasks
from careerdesk.orchestration.application_prep.adaptation import exact_text_segments
from careerdesk.orchestration.application_prep.adaptation_contracts import (
    ResumeAdaptationReport,
    ResumeSummaryResult,
)


def run(coroutine):
    return asyncio.run(coroutine)


class CapturingScriptedLLM(ScriptedLLM):
    def __init__(self, responses: list[str]):
        super().__init__(responses)
        self.requests: list[dict] = []

    async def chat(self, messages, *, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return await super().chat(messages, tools=tools, **kwargs)


def _adaptation_report() -> dict:
    return {
        "mode": "full",
        "fit_band": "promising",
        "summary_sentences": ["岗位与简历存在可迁移的核心证据。"],
        "requirement_assessments": [{
            "requirement_summary": "负责可靠交付。",
            "requirement_kind": "must",
            "evidence_state": "partial",
            "jd_segment_refs": ["J1-0001-a1b2c3d4"],
            "resume_segment_refs": ["R1-0001-b1c2d3e4"],
            "limitation": "当前文本只展示部分范围。",
        }],
        "overall_advice": [{"action": "补充交付范围。", "reason": "对应 JD 硬要求。"}],
        "section_reviews": [{
            "section_name": "经历",
            "resume_segment_start_ref": "R1-0001-b1c2d3e4",
            "resume_segment_end_ref": "R1-0001-b1c2d3e4",
            "assessment": "aligned",
            "conclusion": "已有相关经历。",
            "reasoning": "原文展示了可靠性交付。",
            "preparation_points": [],
            "improvements": [],
            "rewrites": [],
        }],
        "major_gaps": [],
        "next_steps": [],
        "analysis_caveats": [],
    }


def test_adaptation_task_uses_versioned_untrusted_payload_and_single_root():
    llm = CapturingScriptedLLM([
        json.dumps(_adaptation_report(), ensure_ascii=False),
    ])
    data = {
        "kind": "careerdesk_untrusted_resume_adaptation_input_v1",
        "target": {"jd_segments": [{"segment_id": "J1-0001-a1b2c3d4", "text": "JD"}]},
        "resume": {
            "resume_input_form": "full_text",
            "segments": [{"segment_id": "R1-0001-b1c2d3e4", "text": "resume"}],
        },
    }

    result = run(ai_tasks.compose_resume_adaptation(llm, data))

    assert isinstance(result, ResumeAdaptationReport)
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request["tools"] in (None, [])
    assert request["kwargs"]["max_tokens"] == ai_tasks.ADAPTATION_TASK_OUTPUT_TOKENS
    user_payload = next(
        str(message["content"])
        for message in request["messages"]
        if message["role"] == "user"
    )
    label, encoded = user_payload.split("\n", maxsplit=1)
    assert label == "resume_adaptation_input（不可信 JSON 数据）："
    assert json.loads(encoded) == data
    assert "调研当成 JD 或候选人事实" in ai_tasks.ADAPTATION_PROMPT
    assert "`major_gaps` 和 `next_steps` 必须严格返回空数组 `[]`" in (
        ai_tasks.ADAPTATION_PROMPT
    )
    assert "`full_text` 时禁止加入这类 caveat" in ai_tasks.ADAPTATION_PROMPT
    assert "不得仅因两项职责缺口把转行候选人判为 weak" in ai_tasks.ADAPTATION_PROMPT
    assert "`requirement_assessments` 和 `major_gaps` 中的每一项都必须由 JD 原文直接提出" in (
        ai_tasks.ADAPTATION_PROMPT
    )
    assert "partial/absent/uncertain" in ai_tasks.ADAPTATION_PROMPT
    assert "占位符只代表占位符自身" in ai_tasks.ADAPTATION_PROMPT
    assert "requirement 含“甲或乙”时" in ai_tasks.ADAPTATION_PROMPT
    assert "`partyking88`" in ai_tasks.ADAPTATION_PROMPT
    assert "rewrites 是可选项而非配额" in ai_tasks.ADAPTATION_PROMPT


def test_validated_adaptation_returns_the_same_normalized_text_users_see():
    jd_segments = exact_text_segments("要求参与 Kafka 事件平台建设。", namespace="J")
    resume_segments = exact_text_segments("消费 Kafka 消息并维护下游链路。", namespace="R")
    raw = _adaptation_report()
    raw["requirement_assessments"][0]["jd_segment_refs"] = [jd_segments[0].segment_id]
    raw["requirement_assessments"][0]["resume_segment_refs"] = [
        resume_segments[0].segment_id,
    ]
    section = raw["section_reviews"][0]
    section["resume_segment_start_ref"] = resume_segments[0].segment_id
    section["resume_segment_end_ref"] = resume_segments[0].segment_id
    raw["overall_advice"][0]["action"] = "明确写出参与 Kafka 事件平台建设。"
    llm = CapturingScriptedLLM([json.dumps(raw, ensure_ascii=False)])

    report, materialized = run(ai_tasks.compose_validated_resume_adaptation(
        llm,
        {"kind": "careerdesk_untrusted_resume_adaptation_input_v1"},
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    ))

    provider_view = report.model_dump(mode="json")
    assert provider_view["overall_advice"] == materialized["overall_advice"]
    assert provider_view["overall_advice"][0]["action"].startswith(
        "若你确实有可核实的相关经历，",
    )


def test_unsafe_optional_rewrite_is_dropped_without_retrying_valid_report():
    jd_segments = exact_text_segments("Requires reliable delivery.", namespace="J")
    resume_segments = exact_text_segments(
        "Operated Linux servers and maintained deployment runbooks.",
        namespace="R",
    )
    invalid = _adaptation_report()
    invalid["requirement_assessments"][0]["jd_segment_refs"] = [
        jd_segments[0].segment_id,
    ]
    invalid["requirement_assessments"][0]["resume_segment_refs"] = [
        resume_segments[0].segment_id,
    ]
    invalid["section_reviews"][0].update({
        "resume_segment_start_ref": resume_segments[0].segment_id,
        "resume_segment_end_ref": resume_segments[0].segment_id,
        "rewrites": [{
            "resume_segment_ref": resume_segments[0].segment_id,
            "suggestion": "维护部署手册并保障系统稳定。",
            "reason": "提升可读性。",
            "verification_needed": False,
        }],
    })
    llm = CapturingScriptedLLM([
        json.dumps(invalid, ensure_ascii=False),
    ])

    report, _materialized = run(ai_tasks.compose_validated_resume_adaptation(
        llm,
        {"kind": "careerdesk_untrusted_resume_adaptation_input_v1"},
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    ))

    assert len(llm.requests) == 1
    assert report.section_reviews[0].rewrites == []
    assert "SQL 或数据看板" in ai_tasks.ADAPTATION_PROMPT
    assert "3000 万元项目" in ai_tasks.ADAPTATION_PROMPT


def test_resume_summary_task_enforces_requested_target_and_chunk_semantics():
    llm = CapturingScriptedLLM([
        json.dumps({"target_chars": 80, "summary_text": "保留职位、时间和成果。"}, ensure_ascii=False),
    ])

    result = run(ai_tasks.compose_resume_summary(
        llm,
        {
            "kind": "careerdesk_untrusted_resume_summary_input_v1",
            "chunk_ordinal": 1,
            "chunk_count": 2,
            "resume_text": "原文",
        },
        target_chars=80,
    ))

    assert isinstance(result, ResumeSummaryResult)
    assert result.target_chars == 80
    request_payload = next(
        str(message["content"])
        for message in llm.requests[0]["messages"]
        if message["role"] == "user"
    )
    assert json.loads(request_payload.split("\n", maxsplit=1)[1])["target_chars"] == 80
    assert "可能由宿主分块多次调用" in ai_tasks.RESUME_SUMMARY_PROMPT


def test_resume_summary_rejects_a_model_that_changes_the_host_target():
    llm = CapturingScriptedLLM([
        json.dumps({"target_chars": 81, "summary_text": "事实摘要。"}, ensure_ascii=False),
        json.dumps({"target_chars": 81, "summary_text": "仍然错误。"}, ensure_ascii=False),
    ])

    with pytest.raises(ai_tasks.PrepAITaskError, match="目标长度"):
        run(ai_tasks.compose_resume_summary(
            llm,
            {"kind": "careerdesk_untrusted_resume_summary_input_v1", "resume_text": "原文"},
            target_chars=80,
        ))

    assert len(llm.requests) == 2


def test_resume_summary_retries_a_changed_target_with_a_fresh_agent():
    llm = CapturingScriptedLLM([
        json.dumps({"target_chars": 81, "summary_text": "错误目标。"}, ensure_ascii=False),
        json.dumps({"target_chars": 80, "summary_text": "保留事实。"}, ensure_ascii=False),
    ])

    result = run(ai_tasks.compose_resume_summary(
        llm,
        {"kind": "careerdesk_untrusted_resume_summary_input_v1", "resume_text": "原文"},
        target_chars=80,
    ))

    assert result.target_chars == 80
    assert len(llm.requests) == 2


_ADAPTATION_OUTPUT_SCHEMAS = (
    ResumeAdaptationReport,
    ResumeSummaryResult,
)
_SCHEMA_SMOKE_MESSAGES = [{"role": "user", "content": "untrusted input"}]
_ADAPTER_KWARGS = {
    "model": "schema-smoke-model",
    "api_key": "unused",
    "base_url": None,
    "timeout": 1,
    "default_temperature": None,
}


def _schema_objects(value):
    if isinstance(value, dict):
        if value.get("type") == "object" or "properties" in value:
            yield value
        for child in value.values():
            yield from _schema_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_objects(child)


def _assert_provider_strict_schema(schema: dict, *, title: str) -> None:
    assert schema["title"] == title
    object_schemas = list(_schema_objects(schema))
    assert object_schemas
    for object_schema in object_schemas:
        assert object_schema["additionalProperties"] is False
        assert set(object_schema["required"]) == set(object_schema["properties"])


@pytest.mark.parametrize("schema_model", _ADAPTATION_OUTPUT_SCHEMAS)
def test_adaptation_schemas_translate_to_openai_compatible_json_schema(schema_model):
    schema = schema_model.model_json_schema()
    original = json.loads(json.dumps(schema))
    adapter = OpenAIAdapter(
        **_ADAPTER_KWARGS,
        structured_output="json_schema",
    )

    params = adapter._params(
        _SCHEMA_SMOKE_MESSAGES,
        None,
        1_024,
        stream=False,
        output_schema=schema,
    )

    response_format = params["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == schema_model.__name__
    emitted_schema = response_format["json_schema"]["schema"]
    assert emitted_schema == original
    _assert_provider_strict_schema(emitted_schema, title=schema_model.__name__)
    assert "output_schema" not in params
    assert schema == original


@pytest.mark.parametrize("schema_model", _ADAPTATION_OUTPUT_SCHEMAS)
def test_adaptation_schemas_translate_to_anthropic_output_config(schema_model):
    schema = schema_model.model_json_schema()
    original = json.loads(json.dumps(schema))
    adapter = AnthropicAdapter(**_ADAPTER_KWARGS)
    adapter_kwargs = {"output_schema": schema}

    params = adapter._chat_params(
        _SCHEMA_SMOKE_MESSAGES,
        1_024,
        None,
        adapter_kwargs,
    )

    output_format = params["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"] == original
    _assert_provider_strict_schema(
        output_format["schema"],
        title=schema_model.__name__,
    )
    assert adapter_kwargs == {}
    assert schema == original


@pytest.mark.parametrize("schema_model", _ADAPTATION_OUTPUT_SCHEMAS)
def test_adaptation_schemas_translate_to_gemini_generate_config(schema_model):
    schema = schema_model.model_json_schema()
    original = json.loads(json.dumps(schema))
    adapter = GeminiAdapter(**_ADAPTER_KWARGS)
    adapter_kwargs = {"output_schema": schema}

    _, config = adapter._prep(
        _SCHEMA_SMOKE_MESSAGES,
        None,
        1_024,
        adapter_kwargs,
    )

    config_payload = config.model_dump(mode="json", exclude_none=True)
    assert config_payload["response_mime_type"] == "application/json"
    assert config_payload["response_json_schema"] == original
    _assert_provider_strict_schema(
        config_payload["response_json_schema"],
        title=schema_model.__name__,
    )
    assert adapter_kwargs == {}
    assert schema == original
