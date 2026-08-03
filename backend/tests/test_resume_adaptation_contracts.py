"""Resume-adaptation structured contract and text-budget regressions."""

import pytest
from pydantic import ValidationError

from careerdesk.orchestration.application_prep.adaptation_contracts import (
    ADAPTATION_FULL_MAX_TEXT_CHARS,
    ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS,
    ADAPTATION_TASK_OUTPUT_TOKENS,
    SUMMARY_CONFIRMATION_NOTICE,
    ResumeAdaptationReport,
    ResumeSummaryResult,
)


JD_REF = "J1-0001-a1b2c3d4"
RESUME_REF = "R1-0001-b1c2d3e4"


def _requirement(**overrides) -> dict:
    return {
        "requirement_summary": "负责可靠的交付。",
        "requirement_kind": "must",
        "evidence_state": "strong",
        "jd_segment_refs": [JD_REF],
        "resume_segment_refs": [RESUME_REF],
        "limitation": "证据来自当前简历文本。",
        **overrides,
    }


def _full_report(**overrides) -> dict:
    return {
        "mode": "full",
        "fit_band": "strong",
        "summary_sentences": ["整体适配度较高，最强依据是可靠交付经验。"],
        "requirement_assessments": [_requirement()],
        "overall_advice": [{"action": "突出交付证据。", "reason": "对应核心要求。"}],
        "section_reviews": [],
        "major_gaps": [],
        "next_steps": [],
        "analysis_caveats": [],
        **overrides,
    }


@pytest.mark.parametrize(
    "sentence",
    [
        "适配 v2.1 API 岗位，证据较充分。",
        "指标为 3.14，e.g. forecasting 的经历值得前置。",
        "熟悉 U.S. market 与 Node.js，建议突出相关成果。",
    ],
)
def test_summary_sentence_accepts_abbreviations_versions_and_decimals(sentence):
    report = ResumeAdaptationReport.model_validate(
        _full_report(summary_sentences=[sentence]),
    )

    assert report.summary_sentences == [sentence]


@pytest.mark.parametrize(
    "sentence",
    [
        "第一句。第二句。",
        "First sentence. Second sentence.",
        "没有末尾句界",
        "第一行\n第二行。",
    ],
)
def test_summary_sentence_rejects_multiple_missing_or_multiline_boundaries(sentence):
    with pytest.raises(ValidationError):
        ResumeAdaptationReport.model_validate(
            _full_report(summary_sentences=[sentence]),
        )


def test_single_root_enforces_full_and_gap_branches_but_allows_summarized_full_shape():
    full = ResumeAdaptationReport.model_validate(_full_report(section_reviews=[]))
    assert full.mode == "full" and full.section_reviews == []

    gap = ResumeAdaptationReport.model_validate({
        "mode": "gap_brief",
        "fit_band": "weak",
        "summary_sentences": ["核心岗位职能在当前简历中未见充分证据。"],
        "requirement_assessments": [
            _requirement(
                evidence_state="absent",
                resume_segment_refs=[],
            ),
        ],
        "overall_advice": [],
        "section_reviews": [],
        "major_gaps": [{
            "requirement_summary": "需要完整负责交付。",
            "evidence_state": "absent",
            "jd_segment_refs": [JD_REF],
            "resume_segment_refs": [],
            "basis": "当前简历未展示对应证据。",
        }],
        "next_steps": ["补充可核实的相关经历。"],
        "analysis_caveats": [],
    })
    assert gap.mode == "gap_brief" and gap.fit_band == "weak"

    with pytest.raises(ValidationError, match="full 模式"):
        ResumeAdaptationReport.model_validate(_full_report(fit_band="weak"))
    with pytest.raises(ValidationError, match="gap_brief"):
        ResumeAdaptationReport.model_validate({
            **gap.model_dump(),
            "overall_advice": [{"action": "不允许。", "reason": "gap 应保持短。"}],
        })


def test_full_total_text_budget_is_enforced_across_fields():
    oversized = _full_report(
        requirement_assessments=[
            _requirement(
                requirement_summary="要" * 240,
                limitation="限" * 300,
                resume_segment_refs=[],
            )
            for _index in range(12)
        ],
        overall_advice=[
            {"action": "动" * 400, "reason": "因" * 500}
            for _index in range(5)
        ],
    )

    with pytest.raises(ValidationError, match=f"{ADAPTATION_FULL_MAX_TEXT_CHARS:,}"):
        ResumeAdaptationReport.model_validate(oversized)


def test_summary_contract_enforces_dynamic_target_and_host_echo():
    result = ResumeSummaryResult.model_validate({
        "target_chars": 20,
        "summary_text": "保留事实与时间。",
    })
    assert result.require_requested_target(20) is result

    with pytest.raises(ValidationError, match="target_chars"):
        ResumeSummaryResult.model_validate({
            "target_chars": 4,
            "summary_text": "超过四个字符。",
        })
    with pytest.raises(ValueError, match="不一致"):
        result.require_requested_target(21)


def test_output_reserve_and_summary_confirmation_are_frozen():
    assert ADAPTATION_TASK_OUTPUT_TOKENS == 16_384
    assert ADAPTATION_FULL_REQUIRED_OUTPUT_TOKENS == 16_384
    assert "额外调用模型" in SUMMARY_CONFIRMATION_NOTICE
    assert "可能分块多次" in SUMMARY_CONFIRMATION_NOTICE
