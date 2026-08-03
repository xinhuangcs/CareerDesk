"""Pure resume-adaptation segmentation, payload, validation, and capacity tests."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from careerdesk.orchestration.application_prep.adaptation import (
    AdaptationHostValidationError,
    adaptation_input_hash,
    assess_resume_extraction,
    build_resume_adaptation_payload,
    exact_text_segments,
    jd_has_meaningful_content,
    preflight_adaptation_capacity,
    render_untrusted_json,
    validate_cached_materialized_report,
    validate_and_materialize_report,
)
from careerdesk.orchestration.application_prep.adaptation_contracts import (
    ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
    ADAPTATION_PROMPT_VERSION,
    ResumeAdaptationReport,
)
from careerdesk.platform.ai.structured_tasks import (
    STRUCTURED_CONTEXT_GUARD_TOKENS,
    STRUCTURED_RETRY_INSTRUCTION,
    structured_input_tokens,
)


def _full_report(jd_segments, resume_segments, *, section_end=None, resume_refs=None):
    jd_ref = jd_segments[0].segment_id
    resume_ref = resume_segments[0].segment_id
    return ResumeAdaptationReport.model_validate({
        "mode": "full",
        "fit_band": "strong",
        "summary_sentences": ["简历证据与岗位要求整体契合。"],
        "requirement_assessments": [{
            "requirement_summary": "承担关键交付。",
            "requirement_kind": "must",
            "evidence_state": "strong",
            "jd_segment_refs": [jd_ref],
            "resume_segment_refs": [resume_ref] if resume_refs is None else resume_refs,
            "limitation": "仅依据当前文本。",
        }],
        "overall_advice": [{"action": "前置关键成果。", "reason": "对应核心职责。"}],
        "section_reviews": [{
            "section_name": "经历",
            "resume_segment_start_ref": resume_ref,
            "resume_segment_end_ref": section_end or resume_segments[-1].segment_id,
            "assessment": "highly_aligned",
            "conclusion": "经历与岗位契合。",
            "reasoning": "职责与成果均有明确文字证据。",
            "preparation_points": ["准备说明关键决策。"],
            "improvements": ["把结果放在动作之后。"],
            "rewrites": [{
                "resume_segment_ref": resume_ref,
                "suggestion": "保持原事实并突出结果。",
                "reason": "提升可扫描性。",
                "verification_needed": False,
            }],
        }],
        "major_gaps": [],
        "next_steps": [],
        "analysis_caveats": [],
    })


def _assert_optional_rewrite_dropped(raw, *, jd_segments, resume_segments):
    materialized = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(raw),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert materialized["section_reviews"][0]["rewrites"] == []


def test_exact_segments_round_trip_offsets_ids_and_unicode_boundaries():
    text = "  标题\r\n\r\n" + "x" * 4 + "e\u0301" + "👨\u200d👩\u200d👧\u200d👦" + "\n末尾"
    segments = exact_text_segments(text, namespace="R", max_chars=5)

    assert "".join(segment.text for segment in segments) == text
    assert segments[0].char_start == 0
    assert segments[-1].char_end == len(text)
    assert all(
        current.char_end == following.char_start
        for current, following in zip(segments, segments[1:])
    )
    assert all(segment.segment_id.startswith("R1-") for segment in segments)
    assert not any(
        current.text.endswith("e") and following.text.startswith("\u0301")
        for current, following in zip(segments, segments[1:])
    )
    assert not any(
        current.text.endswith("\u200d") or following.text.startswith("\u200d")
        for current, following in zip(segments, segments[1:])
    )


def test_repeated_segment_text_has_distinct_ordinal_bound_ids():
    segments = exact_text_segments("same\nsame\n", namespace="J", max_chars=5)

    assert [segment.text for segment in segments] == ["same\n", "same\n"]
    assert segments[0].segment_id != segments[1].segment_id
    assert segments[0].segment_id.startswith("J1-0001-")
    assert segments[1].segment_id.startswith("J1-0002-")


def test_maximum_legal_resume_text_is_lossless_and_bounded():
    text = ("长期项目经验与成果 123\n" * 9_000)[:200_000]

    segments = exact_text_segments(text, namespace="R")

    assert "".join(segment.text for segment in segments) == text
    assert segments[-1].char_end == len(text)
    assert all(len(segment.text) <= 1_200 for segment in segments)


@pytest.mark.parametrize(
    ("jd_text", "expected"),
    [
        (None, False),
        ("  \n", False),
        ("示例公司\n后端工程师", False),
        ("负责服务可靠性", True),
        ("要求 3 年经验", True),
        ("负责資料分析", True),
    ],
)
def test_jd_unicode_validation_ignores_exact_metadata_lines(jd_text, expected):
    assert jd_has_meaningful_content(
        jd_text,
        company="示例公司",
        position="后端工程师",
    ) is expected


def test_extraction_quality_gate_is_high_confidence_and_auditable():
    scanned = assess_resume_extraction("", source_suffix="pdf")
    assert scanned.status == "reupload_required"
    assert scanned.reason_codes == (
        "missing_content_text",
        "scanned_pdf_without_text_layer",
    )

    corrupt = assess_resume_extraction("\ufffd" * 4 + "abcd")
    assert corrupt.status == "reupload_required"
    assert "abnormal_replacement_characters" in corrupt.reason_codes

    warning = assess_resume_extraction("可靠交付 abc\ufffd")
    assert warning.status == "usable"
    assert warning.warning_codes == ("replacement_characters_present",)
    assert warning.model_dump()["replacement_char_count"] == 1


def test_payload_contains_only_semantic_data_and_omits_research_in_narrow_mode():
    jd_segments = exact_text_segments("完整 JD 尾段", namespace="J")
    resume_segments = exact_text_segments("完整简历尾段", namespace="R")
    payload = build_resume_adaptation_payload(
        company="示例公司",
        position="工程师",
        department=None,
        jd_segments=jd_segments,
        jd_parsed={"skills": ["Python"]},
        resume_input_form="full_text",
        resume_segments=resume_segments,
        research=None,
    )

    assert payload["target"]["jd_segments"][-1]["text"] == "完整 JD 尾段"
    assert payload["resume"]["segments"][-1]["text"] == "完整简历尾段"
    assert "research" not in payload
    encoded = render_untrusted_json("resume_adaptation_input", payload)
    assert "application_id" not in encoded
    assert "resume_id" not in encoded
    assert "char_start" not in encoded and "char_end" not in encoded

    with pytest.raises(ValueError, match="host-only"):
        build_resume_adaptation_payload(
            company="示例公司",
            position="工程师",
            department=None,
            jd_segments=jd_segments,
            resume_input_form="full_text",
            resume_segments=resume_segments,
            research={
                "company_report": {"source_url": "https://forbidden.example"},
            },
        )

    with pytest.raises(ValueError, match="JSON"):
        build_resume_adaptation_payload(
            company="示例公司",
            position="工程师",
            department=None,
            jd_segments=jd_segments,
            resume_input_form="full_text",
            resume_segments=resume_segments,
            jd_parsed={"non_standard_number": float("nan")},
        )


def test_input_hash_covers_full_resume_and_policy_receipts():
    jd_segments = exact_text_segments("JD", namespace="J")
    resume_segments = exact_text_segments("resume", namespace="R")
    payload = build_resume_adaptation_payload(
        company="A",
        position="B",
        department=None,
        jd_segments=jd_segments,
        resume_input_form="full_text",
        resume_segments=resume_segments,
    )
    first = adaptation_input_hash(
        payload,
        resume_id=7,
        resume_content_text="prefix-A",
        research_fingerprint="snapshot-1",
    )
    tail_changed = adaptation_input_hash(
        payload,
        resume_id=7,
        resume_content_text="prefix-B",
        research_fingerprint="snapshot-1",
    )
    policy_changed = adaptation_input_hash(
        payload,
        resume_id=7,
        resume_content_text="prefix-A",
        research_fingerprint="snapshot-1",
        prompt_version=ADAPTATION_PROMPT_VERSION + 1,
    )

    assert len(first) == 64
    assert len({first, tail_changed, policy_changed}) == 3


def test_host_validator_materializes_trusted_text_and_section_id():
    jd_segments = exact_text_segments("要求A\n要求B", namespace="J", max_chars=4)
    resume_segments = exact_text_segments("经验A\n经验B", namespace="R", max_chars=4)
    report = _full_report(jd_segments, resume_segments)

    materialized = validate_and_materialize_report(
        report,
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )

    requirement = materialized["requirement_assessments"][0]
    assert requirement["jd_evidence"][0]["text"] == jd_segments[0].text
    assert requirement["resume_evidence"][0]["text"] == resume_segments[0].text
    section = materialized["section_reviews"][0]
    assert section["section_id"].startswith("S1-0001-")
    assert section["rewrites"][0]["original_text"] == resume_segments[0].text


def test_cached_materialized_report_is_rederived_and_rejects_all_tampering():
    jd_segments = exact_text_segments("要求A\n要求B", namespace="J", max_chars=4)
    resume_segments = exact_text_segments("经验A\n经验B", namespace="R", max_chars=4)
    materialized = validate_and_materialize_report(
        _full_report(jd_segments, resume_segments),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )

    assert validate_cached_materialized_report(
        materialized,
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    ) == materialized

    root_extra = copy.deepcopy(materialized)
    root_extra["content_text"] = "host-only"
    forged_evidence = copy.deepcopy(materialized)
    forged_evidence["requirement_assessments"][0]["jd_evidence"][0]["text"] = "伪造原文"
    invalid_ref = copy.deepcopy(materialized)
    invalid_ref["requirement_assessments"][0]["jd_segment_refs"] = [
        "J1-9999-deadbeef",
    ]
    forged_rewrite = copy.deepcopy(materialized)
    forged_rewrite["section_reviews"][0]["rewrites"][0]["original_text"] = "伪造原文"

    for tampered in (root_extra, forged_evidence, invalid_ref, forged_rewrite):
        with pytest.raises(AdaptationHostValidationError, match="cached report"):
            validate_cached_materialized_report(
                tampered,
                jd_segments=jd_segments,
                resume_segments=resume_segments,
                resume_input_form="full_text",
            )


def test_cached_report_is_rejected_when_old_rewrite_fails_current_fact_gate():
    jd_segments = exact_text_segments(
        "要求首屏优化和前端监控经验。",
        namespace="J",
    )
    resume_segments = exact_text_segments("开发并维护业务页面。", namespace="R")
    materialized = validate_and_materialize_report(
        _full_report(jd_segments, resume_segments),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    materialized["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "开发并维护业务页面，完成首屏优化。"
    )

    with pytest.raises(AdaptationHostValidationError, match="cached report"):
        validate_cached_materialized_report(
            materialized,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )


def test_cached_report_rejects_unsupported_term_absent_from_jd_and_source_segment():
    jd_segments = exact_text_segments("Improve service reliability.", namespace="J")
    resume_segments = exact_text_segments("Maintained Go APIs.\n", namespace="R")
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Maintained Go APIs reliably."
    )
    materialized = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(raw),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    materialized["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Built AWS infrastructure."
    )

    with pytest.raises(AdaptationHostValidationError, match="cached report"):
        validate_cached_materialized_report(
            materialized,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )


def test_cache_accepts_normalized_advice_and_rejects_legacy_unsafe_text():
    jd_segments = exact_text_segments("要求参与 Kafka 事件平台建设。", namespace="J")
    resume_segments = exact_text_segments("消费 Kafka 消息并维护下游链路。", namespace="R")
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    unsafe_action = "明确写出参与 Kafka 事件平台建设。"
    raw["overall_advice"][0]["action"] = unsafe_action
    normalized = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(raw),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )

    assert validate_cached_materialized_report(
        normalized,
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    ) == normalized

    legacy = copy.deepcopy(normalized)
    legacy["overall_advice"][0]["action"] = unsafe_action
    with pytest.raises(AdaptationHostValidationError, match="cached report"):
        validate_cached_materialized_report(
            legacy,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )


def test_host_validator_drops_fabricated_numbers_and_unpaired_fact_placeholders():
    jd_segments = exact_text_segments("要求可靠交付", namespace="J")
    resume_segments = exact_text_segments("故障恢复时间下降 35%。", namespace="R")

    novel_number = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    novel_number["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "将故障恢复时间改善写成 99%。"
    )
    _assert_optional_rewrite_dropped(
        novel_number, jd_segments=jd_segments, resume_segments=resume_segments,
    )

    missing_placeholder = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    missing_placeholder["section_reviews"][0]["rewrites"][0].update({
        "suggestion": "补充可核实的团队规模。",
        "verification_needed": True,
    })
    _assert_optional_rewrite_dropped(
        missing_placeholder, jd_segments=jd_segments, resume_segments=resume_segments,
    )

    unmarked_placeholder = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    unmarked_placeholder["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "协调[请补充可核实的团队规模]完成交付。"
    )
    _assert_optional_rewrite_dropped(
        unmarked_placeholder, jd_segments=jd_segments, resume_segments=resume_segments,
    )

    supported = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    supported["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "将故障恢复时间下降 35% 前置。"
    )
    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(supported),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )


@pytest.mark.parametrize(
    "suggestion",
    [
        "完成首屏优化并记录结果。",
        "使用代码分割改善加载。",
        "实现懒加载。",
        "建设监控系统。",
        "定期开展合规检查。",
        "指导业务调整。",
        "推动根因整改。",
        "对接反洗钱系统。",
    ],
)
def test_rewrite_drops_jd_only_chinese_facts_outside_placeholder(suggestion):
    jd_segments = exact_text_segments(
        "负责前端性能优化，包括首屏优化、代码分割、懒加载，"
        "并建设监控系统。定期开展合规检查，指导业务调整，"
        "推动根因整改，对接反洗钱系统。",
        namespace="J",
    )
    resume_segments = exact_text_segments(
        "参与前端页面开发与合规材料检查。",
        namespace="R",
    )
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = suggestion

    _assert_optional_rewrite_dropped(
        raw, jd_segments=jd_segments, resume_segments=resume_segments,
    )


@pytest.mark.parametrize(
    ("jd_text", "resume_text", "suggestion"),
    [
        (
            "必须熟练使用 React、TypeScript，并有首屏性能优化和前端监控经验。",
            "使用 React 与 TypeScript 交付三个业务后台，"
            "建立组件库和单元测试规范。",
            "使用 React 与 TypeScript 交付三个业务后台，"
            "负责首屏加载优化（如代码分割、"
            "懒加载）；接入前端监控系统。",
        ),
        (
            "负责监管规则解读、制度修订、合规检查与整改跟踪；"
            "有反洗钱经验优先。",
            "跟踪监管更新并形成影响清单，修订制度并闭环检查发现。",
            "定期输出影响清单并指导业务调整，推动根因整改，"
            "了解反洗钱客户识别流程。",
        ),
    ],
)
def test_rewrite_drops_realistic_jd_derived_specifics(jd_text, resume_text, suggestion):
    jd_segments = exact_text_segments(jd_text, namespace="J")
    resume_segments = exact_text_segments(resume_text, namespace="R")
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = suggestion

    _assert_optional_rewrite_dropped(
        raw, jd_segments=jd_segments, resume_segments=resume_segments,
    )


@pytest.mark.parametrize(
    "suggestion",
    [
        "主导线上故障复盘。",
        "牵头线上故障复盘。",
        "负责线上故障复盘。",
        "Led incident reviews.",
        "Owned incident review follow-ups.",
    ],
)
def test_rewrite_drops_role_strengthening_absent_from_resume(suggestion):
    jd_segments = exact_text_segments(
        "重视故障复盘能力。 Incident review experience is preferred.",
        namespace="J",
    )
    resume_segments = exact_text_segments(
        "参与线上故障复盘。 Participated in incident reviews.",
        namespace="R",
    )
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = suggestion

    _assert_optional_rewrite_dropped(
        raw, jd_segments=jd_segments, resume_segments=resume_segments,
    )


def test_rewrite_allows_resume_grounded_terms_roles_and_placeholder_only_jd_fact():
    jd_segments = exact_text_segments(
        "要求 Kafka 消息处理经验，并关注首屏优化。",
        namespace="J",
    )
    grounded_resume = exact_text_segments(
        "主导 Kafka 消费链路的故障复盘。",
        namespace="R",
    )
    grounded = _full_report(jd_segments, grounded_resume).model_dump(mode="json")
    grounded["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "主导 Kafka 消费链路故障复盘，并前置已有结果。"
    )
    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(grounded),
        jd_segments=jd_segments,
        resume_segments=grounded_resume,
        resume_input_form="full_text",
    )

    page_resume = exact_text_segments("开发并维护业务页面。", namespace="R")
    placeholder = _full_report(jd_segments, page_resume).model_dump(mode="json")
    placeholder["section_reviews"][0]["rewrites"][0].update({
        "suggestion": (
            "开发并维护业务页面。[若确实完成，请补充首屏优化方式]"
        ),
        "verification_needed": True,
    })
    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(placeholder),
        jd_segments=jd_segments,
        resume_segments=page_resume,
        resume_input_form="full_text",
    )


def test_rewrite_drops_unsupported_technology_absent_from_jd_and_resume():
    jd_segments = exact_text_segments("Improve service reliability.", namespace="J")
    resume_segments = exact_text_segments("Maintained Go APIs.\n", namespace="R")
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Built AWS infrastructure."
    )

    _assert_optional_rewrite_dropped(
        raw, jd_segments=jd_segments, resume_segments=resume_segments,
    )


@pytest.mark.parametrize(
    "suggestion",
    [
        "Maintained Rust APIs.",
        "Maintained Go APIs at 99% availability.",
    ],
)
def test_rewrite_drops_fact_moved_from_another_resume_segment(suggestion):
    jd_segments = exact_text_segments("Maintain reliable APIs.", namespace="J")
    resume_segments = exact_text_segments(
        "Maintained Go APIs.\nMaintained Rust APIs at 99%.\n",
        namespace="R",
        max_chars=28,
    )
    assert "Rust" not in resume_segments[0].text
    assert "Rust" in resume_segments[1].text and "99%" in resume_segments[1].text
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = suggestion

    _assert_optional_rewrite_dropped(
        raw, jd_segments=jd_segments, resume_segments=resume_segments,
    )

    safe = copy.deepcopy(raw)
    safe["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Maintained Go APIs reliably."
    )
    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(safe),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )


def test_rewrite_does_not_treat_generic_connective_prose_as_jd_fact():
    jd_segments = exact_text_segments(
        "负责项目交付，与团队协作并持续改进。",
        namespace="J",
    )
    resume_segments = exact_text_segments(
        "参与项目交付，协调团队完成上线。",
        namespace="R",
    )
    report = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    report["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "参与项目交付，与团队协作并突出已有上线结果。"
    )

    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(report),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )


def test_english_technical_fact_requires_resume_support_or_conditional_advice():
    jd_segments = exact_text_segments(
        "Requires Kubernetes and Terraform delivery experience.",
        namespace="J",
    )
    resume_segments = exact_text_segments(
        "Operated Linux servers and maintained deployment runbooks.",
        namespace="R",
    )

    rewrite = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    rewrite["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Operated Kubernetes clusters and maintained deployment runbooks."
    )
    _assert_optional_rewrite_dropped(
        rewrite, jd_segments=jd_segments, resume_segments=resume_segments,
    )

    direct_advice = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    direct_advice["overall_advice"][0]["action"] = (
        "Add Kubernetes delivery experience to the resume."
    )
    direct_advice["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Maintained deployment runbooks and operated Linux servers."
    )
    normalized = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(direct_advice),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert normalized["overall_advice"][0]["action"].startswith(
        "If you have relevant verifiable experience, Add Kubernetes",
    )

    reason_advice = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    reason_advice["overall_advice"][0]["reason"] = (
        "Add Kubernetes delivery details so the gap is easier to assess."
    )
    reason_advice["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Maintained deployment runbooks and operated Linux servers."
    )
    normalized_reason = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(reason_advice),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert normalized_reason["overall_advice"][0]["reason"].startswith(
        "If you have relevant verifiable experience, Add Kubernetes",
    )

    conditional = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    conditional["overall_advice"][0]["action"] = (
        "If confirmed, add the Kubernetes delivery experience to the resume."
    )
    conditional["section_reviews"][0]["rewrites"][0]["suggestion"] = (
        "Maintained deployment runbooks and operated Linux servers."
    )
    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(conditional),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )


def _gap_report(jd_segments, resume_segments, *, next_step):
    return ResumeAdaptationReport.model_validate({
        "mode": "gap_brief",
        "fit_band": "weak",
        "summary_sentences": ["当前简历缺少岗位核心经历。"],
        "requirement_assessments": [{
            "requirement_summary": "事件平台建设经验。",
            "requirement_kind": "must",
            "evidence_state": "absent",
            "jd_segment_refs": [jd_segments[0].segment_id],
            "resume_segment_refs": [],
            "limitation": "简历仅展示消息消费链路。",
        }],
        "overall_advice": [],
        "section_reviews": [],
        "major_gaps": [{
            "requirement_summary": "事件平台建设经验。",
            "evidence_state": "absent",
            "jd_segment_refs": [jd_segments[0].segment_id],
            "resume_segment_refs": [],
            "basis": "简历未展示事件平台建设经历。",
        }],
        "next_steps": [next_step],
        "analysis_caveats": [],
    })


def test_advice_improvement_and_next_step_normalize_jd_only_fact_condition():
    jd_segments = exact_text_segments(
        "要求参与 Kafka 事件平台建设。",
        namespace="J",
    )
    resume_segments = exact_text_segments(
        "消费 Kafka 消息并维护下游链路。",
        namespace="R",
    )

    advice = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    advice["overall_advice"][0]["action"] = "明确写出参与 Kafka 事件平台建设。"
    materialized_advice = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(advice),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert materialized_advice["overall_advice"][0] == {
        "action": "若你确实有可核实的相关经历，明确写出参与 Kafka 事件平台建设。",
        "reason": advice["overall_advice"][0]["reason"],
    }

    improvement = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    improvement["section_reviews"][0]["improvements"] = [
        "建议在简历中明确提及参与 Kafka 事件平台建设的职责。",
    ]
    materialized_improvement = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(improvement),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert materialized_improvement["section_reviews"][0]["improvements"] == [
        "若你确实有可核实的相关经历，建议在简历中明确提及参与 Kafka 事件平台建设的职责。",
    ]

    materialized_gap = validate_and_materialize_report(
        _gap_report(
            jd_segments,
            resume_segments,
            next_step="在简历中加入参与 Kafka 事件平台建设的经历。",
        ),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert materialized_gap["next_steps"] == [
        "若你确实有可核实的相关经历，在简历中加入参与 Kafka 事件平台建设的经历。",
    ]


@pytest.mark.parametrize(
    "conditional_advice",
    [
        "如果确实参与过 Kafka 事件平台建设，核实后再补充。",
        "若确认相关经历属实，可写明参与 Kafka 事件平台建设。",
        "不要在简历中写入 Kafka 事件平台建设经历。",
    ],
)
def test_advice_allows_conditional_or_negated_jd_only_fact(conditional_advice):
    jd_segments = exact_text_segments(
        "要求参与 Kafka 事件平台建设。",
        namespace="J",
    )
    resume_segments = exact_text_segments(
        "消费 Kafka 消息并维护下游链路。",
        namespace="R",
    )
    report = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    report["overall_advice"][0]["action"] = conditional_advice

    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(report),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )


@pytest.mark.parametrize(
    "unrelated_tail",
    [
        "；如果需要，再调整排版。",
        "；避免增加无关描述。",
        "，如果需要，再调整排版。",
        "，避免增加无关描述。",
    ],
)
def test_unrelated_trailing_condition_or_negation_does_not_exempt_fact_clause(
    unrelated_tail,
):
    jd_segments = exact_text_segments(
        "要求参与 Kafka 事件平台建设。",
        namespace="J",
    )
    resume_segments = exact_text_segments(
        "消费 Kafka 消息并维护下游链路。",
        namespace="R",
    )
    unsafe_action = f"明确写出参与 Kafka 事件平台建设{unrelated_tail}"
    report = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    report["overall_advice"][0]["action"] = unsafe_action

    materialized = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(report),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )

    assert materialized["overall_advice"][0]["action"] == (
        f"若你确实有可核实的相关经历，{unsafe_action}"
    )


def test_prepare_jd_only_case_is_conditioned_but_generic_interview_prep_is_not():
    jd_segments = exact_text_segments("要求准备能源行业项目案例。", namespace="J")
    resume_segments = exact_text_segments("负责通用产品上线。", namespace="R")

    unsafe = validate_and_materialize_report(
        _gap_report(
            jd_segments,
            resume_segments,
            next_step="面试前准备能源行业项目案例。",
        ),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert unsafe["next_steps"][0].startswith("若你确实有可核实的相关经历，")

    generic = validate_and_materialize_report(
        _gap_report(jd_segments, resume_segments, next_step="面试前准备并梳理问题。"),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )
    assert generic["next_steps"] == ["面试前准备并梳理问题。"]


def test_conditional_advice_normalization_never_silently_truncates():
    jd_segments = exact_text_segments("要求参与 Kafka 事件平台建设。", namespace="J")
    resume_segments = exact_text_segments("消费 Kafka 消息并维护下游链路。", namespace="R")
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    stem = "明确写出参与 Kafka 事件平台建设。"
    raw["overall_advice"][0]["action"] = stem + "甲" * (400 - len(stem))
    report = ResumeAdaptationReport.model_validate(raw)

    with pytest.raises(AdaptationHostValidationError, match="归一后超出"):
        validate_and_materialize_report(
            report,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )


@pytest.mark.parametrize(
    ("resume_text", "accepted", "rejected"),
    [
        (
            "Operated Linux servers and maintained deployment runbooks.",
            "Maintained deployment runbooks and operated Linux servers.",
            "维护部署手册并保障系统稳定。",
        ),
        (
            "负责产品路线图并协调团队完成稳定上线。",
            "负责产品路线图，协调团队完成稳定上线。",
            "The product roadmap supported a stable coordinated launch.",
        ),
    ],
)
def test_rewrite_keeps_clear_source_segment_language(resume_text, accepted, rejected):
    jd_segments = exact_text_segments("要求可靠交付。", namespace="J")
    resume_segments = exact_text_segments(resume_text, namespace="R")
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = accepted
    validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(raw),
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="full_text",
    )

    raw["section_reviews"][0]["rewrites"][0]["suggestion"] = rejected
    _assert_optional_rewrite_dropped(
        raw, jd_segments=jd_segments, resume_segments=resume_segments,
    )


def test_host_validator_rejects_unknown_overlap_coverage_and_summary_refs():
    jd_segments = exact_text_segments("要求A\n要求B", namespace="J", max_chars=4)
    resume_segments = exact_text_segments("经验A\n经验B\n经验C", namespace="R", max_chars=4)

    missing_coverage = _full_report(
        jd_segments,
        resume_segments,
        section_end=resume_segments[0].segment_id,
    )
    with pytest.raises(AdaptationHostValidationError, match="覆盖"):
        validate_and_materialize_report(
            missing_coverage,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )

    unknown = _full_report(
        jd_segments,
        resume_segments,
        resume_refs=["R1-9999-deadbeef"],
    )
    with pytest.raises(AdaptationHostValidationError, match="不存在"):
        validate_and_materialize_report(
            unknown,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )

    summary_report = _full_report(jd_segments, resume_segments)
    with pytest.raises(AdaptationHostValidationError, match="摘要形态"):
        validate_and_materialize_report(
            summary_report,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="summarized",
        )


def test_summarized_host_validation_requires_explicit_summary_gap_provenance():
    jd_segments = exact_text_segments("要求可靠交付", namespace="J")
    resume_segments = exact_text_segments("负责项目交付", namespace="R")
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["requirement_assessments"][0].update({
        "evidence_state": "partial",
        "resume_segment_refs": [],
        "limitation": "压缩摘要中未见完整证据，可能因摘要丢失。",
    })
    raw["section_reviews"] = []
    summarized = ResumeAdaptationReport.model_validate(raw)

    validate_and_materialize_report(
        summarized,
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="summarized",
    )

    missing_phrase = summarized.model_copy(deep=True)
    missing_phrase.requirement_assessments[0].limitation = "当前材料未展示完整证据。"
    with pytest.raises(AdaptationHostValidationError, match="压缩摘要中未见"):
        validate_and_materialize_report(
            missing_phrase,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="summarized",
        )

    gap_raw = summarized.model_dump(mode="json")
    gap_raw.update({
        "mode": "gap_brief",
        "fit_band": "weak",
        "overall_advice": [],
        "major_gaps": [{
            "requirement_summary": "可靠交付经验",
            "evidence_state": "absent",
            "jd_segment_refs": [jd_segments[0].segment_id],
            "resume_segment_refs": [],
            "basis": "压缩摘要中未见可靠交付证据，建议核对原文。",
        }],
        "next_steps": ["核对原文后再决定是否投递。"],
    })
    gap_report = ResumeAdaptationReport.model_validate(gap_raw)
    validate_and_materialize_report(
        gap_report,
        jd_segments=jd_segments,
        resume_segments=resume_segments,
        resume_input_form="summarized",
    )
    gap_report.major_gaps[0].basis = "当前材料未展示可靠交付证据。"
    with pytest.raises(AdaptationHostValidationError, match="major_gap basis"):
        validate_and_materialize_report(
            gap_report,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="summarized",
        )
    gap_report.major_gaps[0].basis = "压缩摘要中未见可靠交付证据。"
    gap_report.major_gaps[0].requirement_summary = "风控协作经验（研究提及）"
    with pytest.raises(AdaptationHostValidationError, match="升级为岗位要求"):
        validate_and_materialize_report(
            gap_report,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="summarized",
        )


@pytest.mark.parametrize(
    "caveat",
    ["使用压缩摘要分析。", "基于简历摘要。", "摘要形态可能遗漏。", "摘要中没有证据。"],
)
def test_full_text_host_validation_rejects_summary_claims(caveat):
    jd_segments = exact_text_segments("要求可靠交付", namespace="J")
    resume_segments = exact_text_segments("负责项目交付", namespace="R")
    report = _full_report(jd_segments, resume_segments).model_copy(deep=True)
    report.analysis_caveats = [caveat]

    with pytest.raises(AdaptationHostValidationError, match="full_text"):
        validate_and_materialize_report(
            report,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )


@pytest.mark.parametrize("marker", ["调研提及", "研究提及", "调研要求", "研究要求"])
def test_host_validation_rejects_research_only_requirement_labels(marker):
    jd_segments = exact_text_segments("要求可靠交付", namespace="J")
    resume_segments = exact_text_segments("负责项目交付", namespace="R")
    report = _full_report(jd_segments, resume_segments).model_copy(deep=True)
    report.requirement_assessments[0].requirement_summary = f"风控协作经验（{marker}）"

    with pytest.raises(AdaptationHostValidationError, match="升级为岗位要求"):
        validate_and_materialize_report(
            report,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )


def test_host_validator_rejects_overlapping_ranges_and_tampered_segment_ids():
    jd_segments = exact_text_segments("要求A\n要求B", namespace="J", max_chars=4)
    resume_segments = exact_text_segments("经验A\n经验B\n经验C", namespace="R", max_chars=4)
    raw = _full_report(jd_segments, resume_segments).model_dump(mode="json")
    raw["section_reviews"] = [
        {
            **raw["section_reviews"][0],
            "resume_segment_end_ref": resume_segments[1].segment_id,
        },
        {
            **raw["section_reviews"][0],
            "section_name": "重叠区块",
            "resume_segment_start_ref": resume_segments[1].segment_id,
            "resume_segment_end_ref": resume_segments[-1].segment_id,
            "rewrites": [],
        },
    ]
    overlapping = ResumeAdaptationReport.model_validate(raw)

    with pytest.raises(AdaptationHostValidationError, match="重叠"):
        validate_and_materialize_report(
            overlapping,
            jd_segments=jd_segments,
            resume_segments=resume_segments,
            resume_input_form="full_text",
        )

    tampered = [replace(resume_segments[0], segment_id="R1-0001-deadbeef")]
    with pytest.raises(AdaptationHostValidationError, match="内容摘要"):
        validate_and_materialize_report(
            _full_report(jd_segments, resume_segments),
            jd_segments=jd_segments,
            resume_segments=tampered,
            resume_input_form="full_text",
        )


def _budget_input(system_prompt: str, payload: str) -> int:
    return structured_input_tokens(
        system_prompt,
        f"{payload}\n\n{STRUCTURED_RETRY_INSTRUCTION}",
        ResumeAdaptationReport,
    )


def test_capacity_preflight_reserves_full_output_and_drops_jd_parsed_first():
    system_prompt = "adapt"
    without_parsed = "small"
    with_parsed = "x" * 30_000
    small_input = _budget_input(system_prompt, without_parsed)
    llm = SimpleNamespace(
        context_window=max(
            2 * ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
            small_input
            + STRUCTURED_CONTEXT_GUARD_TOKENS
            + ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
        ),
        max_output_tokens=ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
    )

    receipt = preflight_adaptation_capacity(
        llm,
        system_prompt=system_prompt,
        payload_with_jd_parsed=with_parsed,
        payload_without_jd_parsed=without_parsed,
    )

    assert receipt.fits is True
    assert receipt.reason == "jd_parsed_dropped"
    assert receipt.include_jd_parsed is False
    assert receipt.available_output_tokens == ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS


def test_capacity_preflight_only_offers_summary_when_no_resume_payload_fits():
    system_prompt = "adapt"
    no_resume = "small"
    no_resume_input = _budget_input(system_prompt, no_resume)
    llm = SimpleNamespace(
        context_window=max(
            2 * ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
            no_resume_input
            + STRUCTURED_CONTEXT_GUARD_TOKENS
            + ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
        ),
        max_output_tokens=ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
    )

    receipt = preflight_adaptation_capacity(
        llm,
        system_prompt=system_prompt,
        payload_with_jd_parsed="r" * 30_000,
        payload_without_jd_parsed="r" * 30_000,
        payload_without_resume=no_resume,
    )

    assert receipt.fits is False
    assert receipt.reason == "resume_only_overflow"
    assert receipt.summarization_available is True


def test_capacity_preflight_unknown_model_fails_closed_without_guessing():
    receipt = preflight_adaptation_capacity(
        SimpleNamespace(context_window=None, max_output_tokens=None),
        system_prompt="adapt",
        payload_with_jd_parsed="payload",
    )

    assert receipt.fits is False
    assert receipt.reason == "missing_model_capacity"
    assert receipt.estimated_input_tokens is None
