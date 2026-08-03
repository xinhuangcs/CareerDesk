"""Deterministic contract for the opt-in real-model evaluation tool."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from careerdesk.agentic.runtime import DEFAULT_SKILL_NAMES
from careerdesk.features.reviews.ai_models import ReviewExtraction


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
EVAL_ROOT = REPOSITORY_ROOT / "ai-evals"

ALL_SUITE_CASE_FILES = {
    "adaptation.json", "agent.json", "extraction.json", "grill.json",
    "grounding.json", "plan.json", "position.json", "quality.json",
    "questions.json", "resume.json", "routing.json",
}
QUALITY_TASKS = {
    "grill_feedback", "question_set_generation", "company_report",
}
PROPOSAL_TYPES = {"intake", "application_delete", "review_undo", "application_merge"}
EXPECTED_KEYS = {
    "agent": {
        "final_output_contains", "proposal_types", "business_tables_unchanged",
        "pending_review_records", "approve_pending_review", "post_approval",
        "applications_count", "pending_extraction_fields", "pending_review_identities",
    },
    "plan": {
        "anchor_domain_contains", "anchor_domain_empty", "anchor_confidence",
        "any_query_mentions_company", "query_must_not_contain",
    },
    "position": {"empty_material_abstention", "tech_question_keywords_any"},
    "grill": {"verdict", "verdict_not_meets", "stuck", "follow_up_null"},
    "resume": {
        "family", "selected_within", "must_not_select", "lines_empty",
    },
    "adaptation": {
        "fit_band_any", "requirement_keyword_any", "requirement_keyword_groups",
        "requirement_evidence_states",
        "gap_keyword_any", "gap_keyword_groups", "advice_keyword_any",
        "advice_keyword_groups", "evidence_text", "analysis_language",
        "rewrite_language", "rewrite_required", "rewrite_numeric_facts_grounded",
        "research_only_not_requirement", "gap_brief", "summary_required_facts",
        "summary_gap_caveat", "resume_refs_empty", "must_not_contain",
    },
}
AGENT_POST_APPROVAL_KEYS = {
    "applications_count", "application_company", "application_projection",
    "real_question_keyword",
}
REVIEW_EXTRACTION_KEYS = set(ReviewExtraction.model_fields)
EXTRACTION_EXPECTED_PATHS = {
    "company", "position", "channel", "history.step", "history.date",
    "history.outcome", "projected_state.stage", "projected_state.current_step",
    "clear_next_action", "next_action",
    "next_action.stage", "next_action.step", "next_action.date", "next_action.time",
    "next_action.note", "mood", "time_of_day", "factors",
}


def _load_evaluator():
    spec = importlib.util.spec_from_file_location(
        "careerdesk_ai_metrics_evaluator",
        EVAL_ROOT / "evaluator.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_runner():
    sys.path.insert(0, str(EVAL_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(
            "careerdesk_ai_metrics_runner",
            EVAL_ROOT / "run.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EVAL_ROOT))


def _cases(name: str) -> list[dict]:
    return json.loads((EVAL_ROOT / f"cases/{name}.json").read_text(encoding="utf-8"))


def test_eval_host_validation_errors_keep_only_the_safe_category():
    evaluator = _load_evaluator()
    host_error = evaluator._error_result(
        "adaptation",
        "synthetic",
        0.0,
        evaluator.AdaptationHostValidationError(
            "rewrite 引用不在所属 section range 内",
        ),
    )
    provider_error = evaluator._error_result(
        "adaptation",
        "synthetic",
        0.0,
        RuntimeError("provider body must stay private"),
    )

    assert host_error["validation_error_code"] == (
        "rewrite 引用不在所属 section range 内"
    )
    assert "validation_error_code" not in provider_error
    assert "provider body" not in json.dumps(provider_error)


def test_eval_classifies_wrapped_provider_schema_failure_without_leaking_details():
    evaluator = _load_evaluator()
    wrapped = evaluator.PrepAITaskError("模型未能生成合规报告")
    wrapped.__cause__ = evaluator.LLMResponseError(
        "private provider output and validation detail",
    )

    result = evaluator._error_result("adaptation", "synthetic", 0.0, wrapped)

    assert result["failure_stage"] == "provider_schema"
    assert "validation_error_code" not in result
    serialized = json.dumps(result)
    assert "private provider output" not in serialized
    assert "validation detail" not in serialized


def test_routing_dataset_is_bounded_unique_and_uses_registered_contract_names():
    cases = _cases("routing")
    allowed_tools = {
        "delete_application",
        "manage_review",
        "parse_jobs",
        "preferences",
        "query_grill",
        "query_library",
        "query_prep",
        "query_status",
        "query_study",
        "query_timeline",
        "record_review",
        "update_application",
    }

    required_keys = {
        "id", "prompt", "expected_first_skill", "expected_first_business_tool",
        "expected_arguments",
    }
    assert 15 <= len(cases) <= 30
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["prompt"] for case in cases}) == len(cases)
    assert all(required_keys <= set(case) <= required_keys | {"seed_reviews"}
               for case in cases)
    assert all(case["expected_first_skill"] in {*DEFAULT_SKILL_NAMES, None} for case in cases)
    assert all(case["expected_first_business_tool"] in {*allowed_tools, None} for case in cases)
    assert any(case["expected_first_skill"] is not None for case in cases)
    assert any(case["expected_first_business_tool"] is None for case in cases)
    assert all(isinstance(case["expected_arguments"], dict) for case in cases)
    assert any(case["expected_arguments"] for case in cases)
    allowed_expected_arguments = {
        "record_review": set(),
        "update_application": {"updates"},
        "manage_review": {
            "action", "company", "position", "new_step", "new_date", "new_outcome",
        },
        "delete_application": {"company", "position", "scope", "targets"},
        "preferences": {"action"},
        "parse_jobs": set(),
    }
    assert all(
        set(case["expected_arguments"]).issubset(
            allowed_expected_arguments.get(case["expected_first_business_tool"], set()),
        )
        for case in cases
    )
    seeded = [case for case in cases if case.get("seed_reviews")]
    assert seeded, "改状态/删除类路由用例应带种子，避免在空库上要求修改不存在的对象"
    for case in seeded:
        for seed in case["seed_reviews"]:
            assert seed["text"].strip()
            assert set(seed["extraction"]) == REVIEW_EXTRACTION_KEYS
            ReviewExtraction.model_validate(seed["extraction"])


def test_all_eval_code_cases_and_generated_data_are_root_scoped():
    assert (EVAL_ROOT / "run.py").is_file()
    assert (EVAL_ROOT / "evaluator.py").is_file()
    assert not (BACKEND_ROOT / "evals/real_model/test_real_model_eval.py").exists()
    assert set(path.name for path in (EVAL_ROOT / "cases").glob("*.json")) == (
        ALL_SUITE_CASE_FILES
    )
    assert (EVAL_ROOT / "data/.gitignore").read_text(encoding="utf-8") == "*\n!.gitignore\n"
    assert "ai-evals/config.json" in (REPOSITORY_ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )


def test_non_routing_datasets_are_bounded_and_self_consistent():
    extraction = _cases("extraction")
    grounding = _cases("grounding")

    assert 3 <= len(extraction) <= 12
    assert len({case["id"] for case in extraction}) == len(extraction)
    assert all(case["expected_fields"] for case in extraction)
    assert all(
        field in EXTRACTION_EXPECTED_PATHS
        for case in extraction
        for field in case["expected_fields"]
    )
    assert 3 <= len(grounding) <= 10
    assert len({case["id"] for case in grounding}) == len(grounding)


def test_task_suite_datasets_are_bounded_with_whitelisted_expectations():
    for suite in ("plan", "position", "grill", "resume", "questions"):
        cases = _cases(suite)
        assert 3 <= len(cases) <= (20 if suite == "questions" else 10), suite
        assert len({case["id"] for case in cases}) == len(cases), suite
        for case in cases:
            expected_keys = set(case.get("expected", {}))
            has_contains = bool(case.get("must_contain") or case.get("must_not_contain"))
            if suite in EXPECTED_KEYS:
                assert expected_keys <= EXPECTED_KEYS[suite], (suite, case["id"])
            assert expected_keys or has_contains, (suite, case["id"])

    for case in _cases("questions"):
        assert case["edition"] in {"basic", "custom"}
        assert 1 <= case["question_limit"] <= 30
        assert case["materials"]
    for case in _cases("resume"):
        line_count = len(case["lines"])
        expected = case["expected"]
        for index in expected.get("selected_within", []):
            assert 0 <= index < line_count
        for index in expected.get("must_not_select", []):
            assert 0 <= index < line_count
    for case in _cases("grill"):
        assert {
            "text", "category", "channel", "evaluation_kind", "answer_authority",
            "rubric", "answer_guide", "evidence",
        } <= set(case["item"])


def test_adaptation_gold_dataset_is_bounded_representative_and_synthetic():
    cases = _cases("adaptation")

    assert 12 <= len(cases) <= 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(set(case["expected"]) <= EXPECTED_KEYS["adaptation"] for case in cases)
    assert all(case["metadata"]["industry"] for case in cases)
    assert all(case["metadata"]["role_family"] for case in cases)
    assert all(case["metadata"]["seniority"] for case in cases)
    assert all(case.get("jd_text", "").strip() for case in cases)
    assert all(case.get("resume_text", "").strip() for case in cases)
    assert all(
        type(case.get("resume_repeat", 1)) is int
        and 1 <= case.get("resume_repeat", 1) <= 200
        for case in cases
    )
    assert {case.get("research_mode", "snapshot") for case in cases} == {
        "snapshot", "none",
    }
    assert {case.get("resume_input_form", "full_text") for case in cases} == {
        "full_text", "summarized",
    }
    families = {case["metadata"]["role_family"] for case in cases}
    assert {"backend", "product", "sales", "operations", "design", "finance", "compliance"} <= families
    assert {case["metadata"]["language"] for case in cases} >= {"zh", "en_resume_zh_ui"}
    assert {case["metadata"]["seniority"] for case in cases} >= {
        "new_grad", "senior", "manager", "career_switch",
    }
    assert {
        band
        for case in cases
        for band in case["expected"].get("fit_band_any", [])
    } >= {"strong", "promising", "weak"}
    assert {
        case.get("research", {}).get("coverage_quality")
        for case in cases
        if case.get("research_mode", "snapshot") == "snapshot"
    } >= {"complete", "partial", "insufficient"}
    assert any("injection" in case["id"] for case in cases)
    assert any(case.get("research", {}).get("source_conflicts") for case in cases)
    assert all(
        case["expected"].get("gap_brief")
        for case in cases
        if case["expected"].get("fit_band_any") == ["weak"]
    )
    assert any(
        case["expected"].get("rewrite_required")
        and case["expected"].get("rewrite_numeric_facts_grounded")
        for case in cases
    )
    assert any(case["expected"].get("research_only_not_requirement") for case in cases)
    assert any(case["expected"].get("advice_keyword_groups") for case in cases)
    for case in cases:
        for expectation in case["expected"].get("requirement_evidence_states", []):
            assert set(expectation) == {"id", "keywords", "allowed_states"}
            assert isinstance(expectation["id"], str) and expectation["id"]
            assert expectation["keywords"]
            assert set(expectation["allowed_states"]) <= {
                "strong", "partial", "absent", "uncertain",
            }

    generated_summaries = [case for case in cases if case.get("generate_summary")]
    assert len(generated_summaries) == 1
    summary_case = generated_summaries[0]
    assert summary_case["resume_input_form"] == "summarized"
    assert not summary_case.get("resume_summary_text")
    assert len(summary_case["resume_text"]) * summary_case["resume_repeat"] >= 12_000
    assert 1 <= summary_case["summary_target_chars"] <= 8_000
    assert summary_case["summary_trigger_context_window"] >= 1_024
    assert summary_case["expected"]["summary_required_facts"]
    assert summary_case["expected"]["summary_gap_caveat"] is True
    serialized = json.dumps(cases, ensure_ascii=False)
    assert "真实用户" not in serialized


def test_adaptation_evaluator_builds_production_inputs_for_both_forms():
    evaluator = _load_evaluator()
    full_case = _cases("adaptation")[0]

    payload, jd_segments, resume_segments, input_form = evaluator._adaptation_inputs(
        full_case,
    )

    assert input_form == "full_text"
    assert "".join(segment.text for segment in jd_segments) == full_case["jd_text"]
    assert "".join(segment.text for segment in resume_segments) == full_case["resume_text"]
    assert payload["kind"] == "careerdesk_untrusted_resume_adaptation_input_v1"
    assert payload["resume"]["resume_input_form"] == "full_text"
    assert payload["research"] == full_case["research"]

    no_research = next(
        case for case in _cases("adaptation") if case.get("research_mode") == "none"
    )
    no_research_payload, _, _, _ = evaluator._adaptation_inputs(no_research)
    assert "research" not in no_research_payload

    summarized = next(
        case
        for case in _cases("adaptation")
        if case.get("resume_input_form") == "summarized"
    )
    summary_payload, _, summary_segments, summary_form = evaluator._adaptation_inputs(
        summarized,
        generated_summary_text="8 年工程项目经验，负责 3000 万元设备改造项目，持有 PMP 证书。",
    )
    assert summary_form == "summarized"
    assert summary_segments == []
    assert summary_payload["resume"] == {
        "resume_input_form": "summarized",
        "summary_text": "8 年工程项目经验，负责 3000 万元设备改造项目，持有 PMP 证书。",
    }

    trigger = evaluator._summary_trigger_receipt(summarized)
    assert trigger.fits is False
    assert trigger.reason == "resume_only_overflow"
    assert trigger.summarization_available is True

    summary_assertions = evaluator._summary_generation_assertions(
        summarized,
        "8 年工程项目经验，负责 3000 万元设备改造项目，持有 PMP 证书。",
        trigger,
    )
    assert {item["name"] for item in summary_assertions} == {
        "summary_overflow_trigger",
        "summary_required_facts",
        "summary_numeric_facts_grounded",
    }
    assert all(item["passed"] for item in summary_assertions)
    fabricated = evaluator._summary_generation_assertions(
        summarized,
        "8 年工程项目经验，负责 3000 万元项目，结果提升 99%，持有 PMP 证书。",
        trigger,
    )
    assert next(
        item for item in fabricated if item["name"] == "summary_numeric_facts_grounded"
    )["passed"] is False


def test_generated_summary_case_calls_the_real_summary_task_seam(monkeypatch):
    evaluator = _load_evaluator()
    case = next(item for item in _cases("adaptation") if item.get("generate_summary"))
    events = []

    class Recorder:
        context_window = 100_000
        max_output_tokens = 16_384
        calls = []

        @staticmethod
        def summary():
            return {
                "llm_calls": 0,
                "usage_missing_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }

    async def fake_summary(_llm, payload, *, target_chars):
        events.append(("summary", len(payload["resume_text"]), target_chars))
        return SimpleNamespace(
            summary_text="8 年工程项目经验，负责 3000 万元设备改造项目，持有 PMP 证书。",
        )

    async def fake_adaptation(_llm, payload, **_validation_context):
        events.append(("adaptation", payload["resume"]["summary_text"]))
        return SimpleNamespace(model_dump=lambda **_kwargs: {"raw": "report"}), {}

    async def fake_close(_llm):
        return None

    capacity = SimpleNamespace(
        fits=True,
        reason="ok",
        estimated_input_tokens=1,
        available_output_tokens=16_384,
        required_output_tokens=16_384,
        context_window=100_000,
    )
    trigger = SimpleNamespace(
        fits=False,
        reason="resume_only_overflow",
        summarization_available=True,
        estimated_input_tokens=70_000,
        available_output_tokens=0,
        context_window=65_536,
    )
    monkeypatch.setattr(evaluator, "_build_recorded_model", lambda _config: Recorder())
    monkeypatch.setattr(evaluator, "compose_resume_summary", fake_summary)
    monkeypatch.setattr(
        evaluator,
        "compose_validated_resume_adaptation",
        fake_adaptation,
    )
    monkeypatch.setattr(evaluator, "_summary_trigger_receipt", lambda _case: trigger)
    monkeypatch.setattr(evaluator, "_adaptation_capacity_receipt", lambda *_args: capacity)
    monkeypatch.setattr(evaluator, "_adaptation_assertions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(evaluator, "close_llm_client", fake_close)

    result = asyncio.run(
        evaluator.evaluate_adaptation_case(
            case,
            evaluator.ModelConfiguration(
                model="fake",
                context_window=100_000,
                max_output_tokens=16_384,
            ),
        )
    )

    assert events[0] == ("summary", len(case["resume_text"]) * case["resume_repeat"], 1_000)
    assert events[1][0] == "adaptation"
    assert "3000 万元" in events[1][1]
    assert result["passed"] is True
    assert {item["name"] for item in result["assertions"]} == {
        "production_capacity_preflight",
        "summary_overflow_trigger",
        "summary_required_facts",
        "summary_numeric_facts_grounded",
    }


def test_adaptation_assertions_cover_gold_safety_and_language_contracts():
    evaluator = _load_evaluator()
    report = {
        "mode": "full",
        "fit_band": "strong",
        "summary_sentences": ["这份简历与岗位的核心产品要求整体契合。"],
        "requirement_assessments": [
            {
                "requirement_summary": "customer discovery",
                "evidence_state": "strong",
                "resume_segment_refs": ["R1"],
                "limitation": "已有具体用户访谈证据。",
            },
            {
                "requirement_summary": "analytics",
                "evidence_state": "partial",
                "resume_segment_refs": [],
                "limitation": "数据分析方法的细节仍需补充。",
            },
        ],
        "overall_advice": [
            {"action": "补充专业联系方式。", "reason": "方便招聘方及时联系。"},
        ],
        "section_reviews": [
            {
                "section_name": "产品经历",
                "conclusion": "经历与岗位需求对齐。",
                "reasoning": "用户访谈与产品优先级经验有证据。",
                "preparation_points": ["准备访谈方法和决策依据。"],
                "improvements": ["说明分析方法。"],
                "rewrites": [
                    {
                        "suggestion": "Interviewed 24 dispatchers to map exception workflows.",
                        "reason": "保留原有事实并突出用户研究。",
                    },
                ],
            },
        ],
        "major_gaps": [],
        "next_steps": [],
        "analysis_caveats": ["结论仅基于已提供文本。"],
    }
    materialized = {
        **report,
        "requirement_assessments": [
            {**report["requirement_assessments"][0],
             "resume_evidence": [{"text": "Interviewed 24 dispatchers."}]},
            {**report["requirement_assessments"][1], "resume_evidence": []},
        ],
    }
    case = {
        "resume_text": "Interviewed 24 dispatchers and mapped exception workflows.",
        "expected": {
            "fit_band_any": ["strong"],
            "requirement_keyword_any": ["discovery"],
            "requirement_keyword_groups": [["discovery"], ["analytics"]],
            "requirement_evidence_states": [{
                "id": "analytics_partial",
                "keywords": ["analytics"],
                "allowed_states": ["partial"],
            }],
            "gap_keyword_any": ["analytics"],
            "gap_keyword_groups": [["analytics"]],
            "advice_keyword_any": ["联系方式"],
            "advice_keyword_groups": [["联系方式"], ["专业"]],
            "evidence_text": "24 dispatchers",
            "must_not_contain": ["ADAPTATION_INJECTION_ACCEPTED"],
            "research_only_not_requirement": ["视频剪辑"],
            "analysis_language": "zh",
            "rewrite_language": "en",
            "rewrite_required": True,
            "rewrite_numeric_facts_grounded": True,
        },
    }

    assertions = evaluator._adaptation_assertions(
        case,
        report,
        materialized,
        resume_input_form="full_text",
    )
    assert {item["name"] for item in assertions} == {
        "fit_band_any",
        "requirement_keyword_any",
        "requirement_keyword_groups",
        "requirement_evidence_state:analytics_partial",
        "gap_keyword_any",
        "gap_keyword_groups",
        "advice_keyword_any",
        "advice_keyword_groups",
        "research_not_upgraded_to_requirement",
        "host_resume_evidence",
        "excludes:ADAPTATION_INJECTION_ACCEPTED",
        "analysis_language:zh",
        "rewrite_language:en",
        "rewrite_required",
        "rewrite_numeric_facts_grounded",
    }
    assert all(item["passed"] for item in assertions)

    summary_report = {
        **report,
        "summary_sentences": ["压缩摘要显示候选人曾管理 3000 万元项目。"],
        "requirement_assessments": [
            {
                **report["requirement_assessments"][0],
                "evidence_state": "absent",
                "resume_segment_refs": [],
                "limitation": "压缩摘要中未见完整证据，建议核对原文。",
            },
        ],
        "section_reviews": [],
    }
    summary_materialized = {
        **summary_report,
        "requirement_assessments": [
            {**summary_report["requirement_assessments"][0], "resume_evidence": []},
        ],
    }
    summary_assertions = evaluator._adaptation_assertions(
        {
            "expected": {
                "evidence_text": "3000 万元",
                "resume_refs_empty": True,
                "summary_gap_caveat": True,
                "analysis_language": "zh",
            },
        },
        summary_report,
        summary_materialized,
        resume_input_form="summarized",
    )
    assert all(item["passed"] for item in summary_assertions)
    assert {item["name"] for item in summary_assertions} == {
        "summary_evidence_text",
        "summary_resume_refs_empty",
        "summary_gap_caveat",
        "analysis_language:zh",
    }


def test_adaptation_weak_gold_exposes_the_short_branch_as_a_named_gate():
    evaluator = _load_evaluator()
    report = {
        "mode": "gap_brief",
        "fit_band": "weak",
        "summary_sentences": ["核心职能与当前简历证据存在明显差距。"],
        "requirement_assessments": [
            {
                "requirement_summary": "SQL 与 A/B 实验",
                "requirement_kind": "must",
                "evidence_state": "absent",
                "jd_segment_refs": ["J1-0001-aaaaaaaa"],
                "resume_segment_refs": [],
                "limitation": "当前简历未展示证据。",
            },
        ],
        "overall_advice": [],
        "section_reviews": [],
        "major_gaps": [
            {
                "requirement_summary": "SQL 与 A/B 实验",
                "evidence_state": "absent",
                "jd_segment_refs": ["J1-0001-aaaaaaaa"],
                "resume_segment_refs": [],
                "basis": "当前简历未展示证据。",
            },
        ],
        "next_steps": ["先补齐可核验的 SQL 与实验项目经历。"],
        "analysis_caveats": ["仅基于已提供文本。"],
    }
    materialized = {
        **report,
        "requirement_assessments": [
            {**report["requirement_assessments"][0], "resume_evidence": []},
        ],
        "major_gaps": [
            {**report["major_gaps"][0], "resume_evidence": []},
        ],
    }

    assertions = evaluator._adaptation_assertions(
        {"expected": {"fit_band_any": ["weak"], "gap_brief": True}},
        report,
        materialized,
        resume_input_form="full_text",
    )

    assert {item["name"] for item in assertions} == {
        "fit_band_any",
        "weak_gap_brief_shape",
    }
    assert all(item["passed"] for item in assertions)


def test_routing_target_selection_allows_read_before_write():
    evaluator = _load_evaluator()
    select = evaluator._select_routing_target

    read_then_write = [
        ("query_timeline", {}),
        ("update_application", {"updates": [{"company": "字节", "new_stage": "offer"}]}),
    ]
    assert select(read_then_write, "update_application") == (
        "update_application", {"updates": [{"company": "字节", "new_stage": "offer"}]},
    )
    parallel_reads = [("query_timeline", {}), ("query_prep", {"action": "list"})]
    assert select(parallel_reads, "query_prep") == ("query_prep", {"action": "list"})
    assert select([("query_status", {})], "query_status") == ("query_status", {})
    assert select([("query_timeline", {})], "query_status") == ("query_timeline", {})
    assert select([("query_timeline", {})], None) == ("query_timeline", {})
    assert select([("query_timeline", {})], "update_application") == (None, None)
    assert select([], "record_review") == (None, None)
    list_then_apply = [
        ("preferences", {"action": "list"}),
        ("preferences", {"action": "apply", "changes": []}),
    ]
    assert select(list_then_apply, "preferences") == (
        "preferences", {"action": "apply", "changes": []},
    )
    assert select([("preferences", {"action": "list"})], "preferences") == (
        "preferences", {"action": "list"},
    )


def test_routing_probe_passes_read_queries_and_halts_writes():
    evaluator = _load_evaluator()
    probe = evaluator.RoutingProbe()

    probe.before_tool("load_skill", {"name": "prepare-for-interview"})
    probe.before_tool("query_timeline", {})
    probe.before_tool("preferences", {"action": "list"})
    with pytest.raises(evaluator.BusinessToolObserved):
        probe.before_tool("preferences", {"action": "apply"})
    assert [name for name, _ in probe.events] == [
        "load_skill", "query_timeline", "preferences", "preferences",
    ]


def test_quality_dataset_uses_versioned_binary_rubrics():
    cases = _cases("quality")

    assert 3 <= len(cases) <= 10
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["task"] for case in cases} <= QUALITY_TASKS
    assert {case["task"] for case in cases} == QUALITY_TASKS
    for case in cases:
        criteria = case["criteria"]
        assert 2 <= len(criteria) <= 6, case["id"]
        assert len({item["id"] for item in criteria}) == len(criteria)
        assert all(item["text"].strip() for item in criteria)
        assert all(set(item) == {"id", "text"} for item in criteria)


def test_agent_dataset_seeds_validate_against_production_extraction_schema():
    cases = _cases("agent")

    assert 3 <= len(cases) <= 10
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["prompt"] for case in cases}) == len(cases)
    for case in cases:
        for seed in case.get("seed_reviews", []):
            assert seed["text"].strip()
            assert set(seed["extraction"]) == REVIEW_EXTRACTION_KEYS
            ReviewExtraction.model_validate(seed["extraction"])
        expected = case["expected"]
        assert set(expected) <= EXPECTED_KEYS["agent"], case["id"]
        assert set(expected.get("post_approval", {})) <= AGENT_POST_APPROVAL_KEYS
        assert set(expected.get("pending_extraction_fields", {})) <= EXTRACTION_EXPECTED_PATHS
        identities = expected.get("pending_review_identities", [])
        assert all(
            isinstance(identity, list)
            and len(identity) == 2
            and all(isinstance(value, str) and value for value in identity)
            for identity in identities
        )
        projection = expected.get("post_approval", {}).get("application_projection")
        if projection is not None:
            assert set(projection) == {
                "company", "position", "stage", "current_step", "next_action",
            }
        assert set(expected.get("proposal_types", [])) <= PROPOSAL_TYPES
        assert expected, case["id"]
    assert any(case["expected"].get("proposal_types") for case in cases)
    assert any(case["expected"].get("approve_pending_review") for case in cases)
    assert any(case["expected"].get("business_tables_unchanged") for case in cases)


def test_model_eval_is_manual_cost_gated_and_outside_ordinary_ci():
    runner = (EVAL_ROOT / "run.py").read_text(encoding="utf-8")
    workflow = (REPOSITORY_ROOT / ".github/workflows/llm-eval.yml").read_text(
        encoding="utf-8"
    )
    automatic_ci = (
        REPOSITORY_ROOT / ".github/workflows/unsigned-release.yml"
    ).read_text(encoding="utf-8")

    assert "acknowledge_model_costs" in runner
    assert "SENSITIVE_KEY" in runner
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert 'python -B "ai-evals/run.py"' in workflow
    assert "path: ai-evals/data/" in workflow
    assert '"ai-evals/run.py"' not in automatic_ci


def test_summary_reports_case_and_field_level_metrics_without_global_model_claims():
    evaluator = _load_evaluator()
    results = [
        {
            "suite": "routing",
            "passed": True,
            "duration_ms": 100,
            "assertions": [
                {"name": "first_skill", "passed": True},
                {"name": "first_business_tool", "passed": True},
                {"name": "business_writes", "passed": True},
                {"name": "tool_argument:action", "passed": True},
            ],
        },
        {
            "suite": "extraction",
            "passed": False,
            "duration_ms": 200,
            "error_type": "TimeoutError",
            "assertions": [
                {"name": "company", "passed": True},
                {"name": "position", "passed": False},
            ],
        },
    ]

    summary = evaluator.summarize(
        results,
        {"routing_accuracy": 0.95, "extraction_field_accuracy": 0.95},
    )

    assert summary["metrics"]["routing_accuracy"]["value"] == 1.0
    assert summary["metrics"]["tool_argument_accuracy"]["value"] == 1.0
    assert summary["metrics"]["extraction_field_accuracy"]["value"] == 0.5
    assert summary["error_count"] == 1
    assert summary["error_rate"] == 0.5
    assert summary["safety_passed"] is True
    assert summary["latency_ms"] == {"p50": 150.0, "p95": 200.0}
    assert summary["all_targets_met"] is False

    missing_metric = evaluator.summarize(
        results,
        {"routing_accuracy": 0.95, "resume_accuracy": 0.85},
    )
    assert missing_metric["all_targets_met"] is False


def test_adaptation_deterministic_safety_failures_are_zero_tolerance():
    evaluator = _load_evaluator()
    results = [{
        "suite": "adaptation",
        "case_id": "unsafe-adaptation",
        "passed": False,
        "duration_ms": 10,
        "assertions": [
            {"name": "requirement_keyword_any", "passed": True},
            {"name": "rewrite_numeric_facts_grounded", "passed": False},
            {"name": "excludes:ADAPTATION_INJECTION_ACCEPTED", "passed": False},
        ],
    }]

    summary = evaluator.summarize(results, {"adaptation_accuracy": 0.0})

    assert summary["metrics"]["adaptation_accuracy"]["target_met"] is True
    assert summary["safety_violation_count"] == 2
    assert summary["safety_passed"] is False


def test_summary_quantifies_extraction_fallback_contribution():
    evaluator = _load_evaluator()

    assert evaluator._matches_expected({"position": None}, "position", None)
    assert not evaluator._matches_expected({}, "position", None)

    contribution = evaluator._fallback_contribution(
        {
            "company": "字节",
            "history": {"outcome": None, "date": None},
            "channel": None,
        },
        {
            "company": "字节",
            "history": {"outcome": "failed", "date": "2026-07-07"},
            "channel": None,
        },
        {"company": "字节", "history.outcome": "failed", "channel": "官网"},
    )
    assert contribution == {
        "changed_fields": ["history.date", "history.outcome"],
        "rescued_fields": ["history.outcome"],
        "harmed_fields": [],
    }

    results = [
        {
            "suite": "extraction",
            "passed": True,
            "duration_ms": 100,
            "assertions": [
                {"name": "company", "passed": True},
                {"name": "history.outcome", "passed": True},
                {"name": "question_0_keywords", "passed": True},
            ],
            "fallback": {
                "changed_fields": ["history.outcome"],
                "rescued_fields": ["history.outcome"],
                "harmed_fields": [],
            },
        },
        {
            "suite": "extraction",
            "passed": False,
            "duration_ms": 100,
            "assertions": [
                {"name": "company", "passed": True},
                {"name": "history.outcome", "passed": False},
            ],
            "fallback": {
                "changed_fields": [],
                "rescued_fields": [],
                "harmed_fields": [],
            },
        },
    ]

    summary = evaluator.summarize(results, {})

    assert summary["metrics"]["extraction_fallback_rescue_share"] == {
        "value": 0.25, "passed": 1, "total": 4, "target": None, "target_met": True,
    }
    assert summary["extraction_fallback"] == {
        "cases_total": 2,
        "cases_touched": 1,
        "rescued_field_count": 1,
        "harmed_field_count": 0,
        "changed_field_names": ["history.outcome"],
    }


def test_summary_aggregates_usage_costs_budget_and_stability():
    evaluator = _load_evaluator()
    results = [
        {
            "suite": "agent",
            "case_id": "a",
            "attempt": 1,
            "passed": True,
            "duration_ms": 100,
            "assertions": [{"name": "final_output_contains:x", "passed": True}],
            "usage": {"llm_calls": 3, "usage_missing_calls": 1,
                      "input_tokens": 1_000, "output_tokens": 200,
                      "total_tokens": 1_200},
        },
        {
            "suite": "grill",
            "case_id": "g",
            "attempt": 1,
            "passed": False,
            "duration_ms": 50,
            "assertions": [{"name": "verdict", "passed": False}],
            "usage": {"llm_calls": 1, "usage_missing_calls": 0,
                      "input_tokens": 500, "output_tokens": 100,
                      "total_tokens": 600},
        },
    ]

    summary = evaluator.summarize(
        results,
        {},
        pricing={"input_per_million": 2.0, "output_per_million": 10.0},
        run_state={
            "max_total_tokens": 100_000,
            "total_tokens_spent": 1_800,
            "budget_exhausted": False,
            "skipped_case_executions": 0,
        },
    )

    assert summary["metrics"]["agent_task_completion"]["value"] == 1.0
    assert summary["metrics"]["grill_accuracy"]["value"] == 0.0
    total = summary["usage"]["total"]
    assert total == {"llm_calls": 4, "usage_missing_calls": 1,
                     "input_tokens": 1_500, "output_tokens": 300,
                     "total_tokens": 1_800}
    assert summary["usage"]["by_suite"]["agent"]["total_tokens"] == 1_200
    assert summary["usage"]["estimated_cost"] == {
        "currency": "USD", "input": 0.003, "output": 0.003, "total": 0.006,
    }
    assert summary["budget"] == {
        "max_total_tokens": 100_000,
        "spent_total_tokens": 1_800,
        "exhausted": False,
        "skipped_case_executions": 0,
    }
    assert summary["distinct_case_count"] == 2
    assert summary["stable_pass_case_count"] == 1
    assert summary["stable_case_pass_rate"] == 0.5


def test_usage_recorder_normalizes_provider_shapes_and_forwards_attributes():
    evaluator = _load_evaluator()

    assert evaluator.normalize_usage({"prompt_tokens": 10, "completion_tokens": 5}) == {
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
    }
    assert evaluator.normalize_usage({"input_tokens": 7, "output_tokens": 3}) == {
        "input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
    }
    assert evaluator.normalize_usage({
        "prompt_token_count": 4, "candidates_token_count": 2, "total_token_count": 9,
    }) == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 9}
    assert evaluator.normalize_usage(None) == {
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
    }

    class FakeLLM:
        context_window = 100_000

        async def chat(self, messages, **kwargs):
            return SimpleNamespace(
                model="fake", latency_ms=12,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                finish_reason="stop", content="hi",
            )

    proxy = evaluator.UsageRecordingLLM(FakeLLM())
    response = asyncio.run(proxy.chat([]))

    assert response.content == "hi"
    assert proxy.context_window == 100_000
    assert proxy.calls[0]["total_tokens"] == 15
    assert proxy.summary() == {
        "llm_calls": 1, "usage_missing_calls": 0,
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
    }


def test_runner_records_dataset_identity_and_bounds_stochastic_repetitions():
    runner = _load_runner()
    config = {
        "model": "anthropic",
        "acknowledge_model_costs": True,
        "repetitions": 3,
    }

    runner._validate_configuration(config)
    manifest, fingerprint, planned = runner._dataset_manifest(config)

    assert config["suites"] == [s for s in runner.VALID_SUITES if s != "quality"]
    assert set(manifest) == set(runner.VALID_SUITES) - {"quality"}
    assert all(len(item["sha256"]) == 64 for item in manifest.values())
    assert len(fingerprint) == 64
    total_cases = sum(item["case_count"] for item in manifest.values())
    assert total_cases == 117
    assert planned == total_cases * 3

    _, smoke_fingerprint, smoke_planned = runner._dataset_manifest(config, smoke=True)
    assert smoke_planned == len(runner.VALID_SUITES) - 1
    assert smoke_fingerprint == fingerprint

    judged = {
        "model": "anthropic",
        "acknowledge_model_costs": True,
        "judge_model": "openai",
        "repetitions": 3,
    }
    runner._validate_configuration(judged)
    judged_manifest, _, judged_planned = runner._dataset_manifest(judged)
    assert judged["suites"] == list(runner.VALID_SUITES)
    assert set(judged_manifest) == set(runner.VALID_SUITES)
    judged_total = sum(item["case_count"] for item in judged_manifest.values())
    assert judged_total == 121
    assert judged_planned == judged_total * 3

    example_config = json.loads(
        (EVAL_ROOT / "config.example.json").read_text(encoding="utf-8")
    )
    assert set(example_config["targets"]) == runner.VALID_TARGETS

    config["repetitions"] = 6
    with pytest.raises(ValueError, match="1 到 5"):
        runner._validate_configuration(config)

    custom_model = {
        "model": "anthropic:not-the-provider-default",
        "acknowledge_model_costs": True,
    }
    with pytest.raises(ValueError, match="具体型号必须填写"):
        runner._validate_configuration(custom_model)
    custom_model.update({"context_window": 4096, "max_output_tokens": 512})
    runner._validate_configuration(custom_model)


def test_release_adaptation_cli_applies_a_non_downgradable_single_suite_gate(
    monkeypatch,
    tmp_path,
):
    runner = _load_runner()
    monkeypatch.setattr(sys, "argv", [
        "run.py",
        "--config", str(tmp_path / "missing-config.json"),
        "--model", "anthropic",
        "--release-adaptation",
        "--acknowledge-costs",
    ])

    arguments = runner._arguments()
    config = runner._read_configuration(arguments)
    runner._validate_configuration(config)

    assert arguments.release_adaptation is True
    assert config["release_adaptation"] is True
    assert config["suites"] == ["adaptation"]
    assert config["repetitions"] == 3
    assert config["enforce_targets"] is True
    assert config["targets"] == {"adaptation_accuracy": 0.90}


def test_release_adaptation_policy_drops_unrelated_targets_and_keeps_stricter_gate():
    runner = _load_runner()
    config = {
        "model": "anthropic",
        "acknowledge_model_costs": True,
        "release_adaptation": True,
        "suites": ["routing", "quality"],
        "repetitions": 4,
        "enforce_targets": False,
        "targets": {
            "routing_accuracy": 1.0,
            "quality_score": 1.0,
            "adaptation_accuracy": 0.95,
        },
    }

    runner._validate_configuration(config)

    assert config["suites"] == ["adaptation"]
    assert config["repetitions"] == 4
    assert config["enforce_targets"] is True
    assert config["targets"] == {"adaptation_accuracy": 0.95}

    with pytest.raises(ValueError, match="smoke"):
        runner._validate_configuration({
            "model": "anthropic",
            "acknowledge_model_costs": True,
            "release_adaptation": True,
            "smoke": True,
        })
    with pytest.raises(ValueError, match="1 到 5"):
        runner._validate_configuration({
            "model": "anthropic",
            "acknowledge_model_costs": True,
            "release_adaptation": True,
            "repetitions": 6,
        })


def test_adaptation_implementation_fingerprint_captures_actual_worktree_bytes():
    runner = _load_runner()

    manifest, fingerprint = runner._implementation_manifest()

    assert tuple(manifest) == runner.ADAPTATION_IMPLEMENTATION_PATHS
    assert {
        "backend/src/careerdesk/features/applications/repository/prep.py",
        "backend/src/careerdesk/features/research/contracts.py",
        "backend/src/careerdesk/orchestration/application_prep/adaptation_workflow.py",
        "backend/src/careerdesk/orchestration/application_prep/api.py",
        "backend/src/careerdesk/orchestration/application_prep/factory.py",
        "backend/src/careerdesk/platform/ai/client.py",
        "backend/src/careerdesk/platform/ai/providers.py",
    } <= set(runner.ADAPTATION_IMPLEMENTATION_PATHS)
    expected_parts = []
    for relative_path in runner.ADAPTATION_IMPLEMENTATION_PATHS:
        payload = (REPOSITORY_ROOT / relative_path).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        assert manifest[relative_path] == {
            "sha256": digest,
            "size_bytes": len(payload),
        }
        expected_parts.append(f"{relative_path}:{digest}")
    assert fingerprint == hashlib.sha256(
        "\n".join(expected_parts).encode("utf-8"),
    ).hexdigest()

    provenance = runner._implementation_provenance({"release_adaptation": True})
    assert provenance["release_adaptation"] is True
    assert provenance["implementation_files"] == manifest
    assert provenance["implementation_fingerprint"] == fingerprint
    assert provenance["implementation_worktree_dirty"] in {True, False, None}
    assert "**_implementation_provenance(config)" in (
        EVAL_ROOT / "run.py"
    ).read_text(encoding="utf-8")


def test_runner_validates_budget_and_pricing_configuration():
    runner = _load_runner()
    base = {"model": "anthropic", "acknowledge_model_costs": True}

    runner._validate_configuration({
        **base,
        "max_total_tokens": 200_000,
        "pricing": {"input_per_million": 3, "output_per_million": 15,
                    "currency": "USD"},
    })

    with pytest.raises(ValueError, match="max_total_tokens"):
        runner._validate_configuration({**base, "max_total_tokens": 5_000})
    with pytest.raises(ValueError, match="pricing.input_per_million"):
        runner._validate_configuration({
            **base,
            "pricing": {"input_per_million": -1, "output_per_million": 1},
        })
    with pytest.raises(ValueError, match="未知字段"):
        runner._validate_configuration({
            **base,
            "pricing": {"input_per_million": 1, "output_per_million": 1,
                        "cache_per_million": 1},
        })
    with pytest.raises(ValueError, match="currency"):
        runner._validate_configuration({
            **base,
            "pricing": {"input_per_million": 1, "output_per_million": 1,
                        "currency": "very-long-currency-name"},
        })


def test_runner_validates_judge_configuration_and_bias_guards():
    runner = _load_runner()
    base = {"model": "anthropic", "acknowledge_model_costs": True}

    with pytest.raises(ValueError, match="judge_model"):
        runner._validate_configuration({**base, "suites": ["quality"]})
    with pytest.raises(ValueError, match="自偏好"):
        runner._validate_configuration({**base, "judge_model": "anthropic"})
    with pytest.raises(ValueError, match="judge_samples"):
        runner._validate_configuration({
            **base, "judge_model": "openai", "judge_samples": 6,
        })
    with pytest.raises(ValueError, match="quality 套件"):
        runner._validate_configuration({
            **base, "pairwise_baseline": "20260101T000000Z-x",
        })

    valid = {**base, "judge_model": "openai", "judge_samples": 5,
             "pairwise_baseline": "20260101T000000Z-x"}
    runner._validate_configuration(valid)
    assert "quality" in valid["suites"]


def test_quality_review_sheet_and_agreement_math(tmp_path):
    runner = _load_runner()
    results = [
        {
            "suite": "quality",
            "case_id": "q1",
            "attempt": 1,
            "passed": False,
            "duration_ms": 10,
            "assertions": [
                {"name": "criterion:specific_gap", "passed": True,
                 "expected": True,
                 "actual": {"votes": [True, True, False], "evidence": "指出了缺口"}},
                {"name": "criterion:no_fabrication", "passed": False,
                 "expected": True,
                 "actual": {"votes": [False, False, True], "evidence": "编造了轮次"}},
            ],
            "candidate_output": {"feedback": "……"},
        },
        {
            "suite": "quality", "case_id": "q2", "attempt": 1, "passed": False,
            "duration_ms": 5, "error_type": "TimeoutError",
            "assertions": [{"name": "completed", "passed": False,
                            "expected": "success", "actual": "TimeoutError"}],
        },
    ]

    sheet = runner._quality_review_sheet(
        results,
        {("q1", "specific_gap"): "feedback 指出具体缺失点"},
    )
    assert [row["criterion_id"] for row in sheet] == [
        "specific_gap", "no_fabrication",
    ]
    assert sheet[0]["criterion_text"] == "feedback 指出具体缺失点"
    assert sheet[0]["judge_passed"] is True
    assert sheet[1]["evidence"] == "编造了轮次"
    assert all(row["human_passed"] is None for row in sheet)

    labeled = [
        {**sheet[0], "human_passed": True},
        {**sheet[1], "human_passed": True},
    ]
    path = tmp_path / "sheet.json"
    path.write_text(json.dumps(labeled, ensure_ascii=False), encoding="utf-8")
    assert runner._agreement_report(path) == 0

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps(sheet, ensure_ascii=False), encoding="utf-8")
    assert runner._agreement_report(empty) == 2


def test_summary_scores_quality_criteria_with_separate_judge_accounting():
    evaluator = _load_evaluator()
    results = [{
        "suite": "quality",
        "case_id": "q1",
        "attempt": 1,
        "passed": False,
        "duration_ms": 10,
        "assertions": [
            {"name": "criterion:a", "passed": True},
            {"name": "criterion:b", "passed": True},
            {"name": "criterion:c", "passed": False},
            {"name": "criterion:d", "passed": False},
        ],
        "usage": {"llm_calls": 1, "usage_missing_calls": 0,
                  "input_tokens": 800, "output_tokens": 200, "total_tokens": 1_000},
        "judge_usage": {"llm_calls": 3, "usage_missing_calls": 0,
                        "input_tokens": 2_400, "output_tokens": 600,
                        "total_tokens": 3_000},
        "pairwise_outcome": "win",
    }]

    summary = evaluator.summarize(
        results,
        {"quality_score": 0.8},
        judge_pricing={"input_per_million": 5.0, "output_per_million": 25.0},
        run_state={"pairwise_baseline": "20260101T000000Z-base"},
    )

    assert summary["metrics"]["quality_score"]["value"] == 0.5
    assert summary["metrics"]["quality_score"]["target_met"] is False
    assert summary["usage"]["total"]["total_tokens"] == 1_000
    assert summary["usage"]["judge_total"]["total_tokens"] == 3_000
    assert summary["usage"]["judge_estimated_cost"]["total"] == 0.027
    assert summary["pairwise"] == {
        "baseline_run": "20260101T000000Z-base",
        "compared_cases": 1,
        "wins": 1,
        "losses": 0,
        "ties": 0,
        "win_rate": 1.0,
    }
    assert evaluator._case_tokens(results[0]) == 4_000


def test_quality_misses_only_block_when_targets_are_enforced():
    runner = _load_runner()
    quality_miss = {
        "error_count": 0,
        "safety_passed": True,
        "all_targets_met": False,
    }

    assert runner._result_exit_code(quality_miss, {"enforce_targets": False}) == 0
    assert runner._result_exit_code(quality_miss, {"enforce_targets": True}) == 1
    assert runner._result_exit_code(
        {**quality_miss, "error_count": 1},
        {"enforce_targets": False},
    ) == 1
    assert runner._result_exit_code(
        {**quality_miss, "safety_passed": False},
        {"enforce_targets": False},
    ) == 1
    assert runner._result_exit_code(
        {
            **quality_miss,
            "budget": {"max_total_tokens": 10_000, "spent_total_tokens": 10_500,
                       "exhausted": True, "skipped_case_executions": 3},
        },
        {"enforce_targets": False},
    ) == 1


def test_model_preflight_constructs_and_closes_without_running_a_case(monkeypatch):
    runner = _load_runner()
    model = object()
    calls = []

    monkeypatch.setattr(runner, "build_llm", lambda *args, **kwargs: model)

    async def close(value):
        calls.append(value)

    monkeypatch.setattr(runner, "close_llm_client", close)
    asyncio.run(runner._preflight_model({
        "model": "anthropic",
        "context_window": None,
        "max_output_tokens": None,
    }))

    assert calls == [model]


def test_summary_exposes_cross_attempt_instability():
    evaluator = _load_evaluator()
    results = [
        {
            "suite": "routing",
            "case_id": "route-one",
            "attempt": 1,
            "passed": True,
            "duration_ms": 10,
            "assertions": [
                {"name": "first_skill", "passed": True},
                {"name": "first_business_tool", "passed": True},
                {"name": "business_writes", "passed": True},
            ],
        },
        {
            "suite": "routing",
            "case_id": "route-one",
            "attempt": 2,
            "passed": False,
            "duration_ms": 20,
            "assertions": [
                {"name": "first_skill", "passed": True},
                {"name": "first_business_tool", "passed": False},
                {"name": "business_writes", "passed": True},
            ],
        },
    ]

    summary = evaluator.summarize(results, {})

    assert summary["unstable_case_count"] == 1
    assert summary["unstable_cases"] == [
        {"suite": "routing", "case_id": "route-one"},
    ]
    assert summary["stable_pass_case_count"] == 0
