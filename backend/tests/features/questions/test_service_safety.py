"""Host-side publication validation for generated question sets."""

import pytest

from careerdesk.features.questions.generation_models import GeneratedQuestionSet
from careerdesk.orchestration.interview_generation.workflow import (
    FrozenInput,
    _bounded_segments,
    _materialize,
)


def _result(ref_id: str = "R1") -> GeneratedQuestionSet:
    return GeneratedQuestionSet.model_validate({
        "questions": [{"text": "请说明尾部成果", "category": "resume_deep_dive",
                       "channel": "interview", "response_format": "oral_text",
                       "difficulty": "introductory", "basis_kinds": ["resume"],
                       "evidence_refs": [{"basis_kind": "resume", "ref_id": ref_id}],
                       "limitations": [], "primary_competency": "表达",
                       "secondary_tags": [], "evaluation_kind": "evidence_consistency",
                       "rubric": {"essential_criteria": ["引用事实"], "quality_signals": [],
                                  "critical_errors": []},
                       "answer_guide": "先说明事实。", "follow_up_allowed": True}],
        "coverage": {"processed_sources": ["resume"], "covered_categories": ["resume_deep_dive"],
                     "omitted_categories": [], "omission_reasons": [], "limitations": []},
    })


def _frozen() -> FrozenInput:
    text = "开头" + "x" * 900 + "唯一尾部成果"
    return FrozenInput("basic", 1, None, "简历", ({"kind": "resume", "hash": "h",
        "segments": _bounded_segments([{"id": "R1", "text": text}])},),
        {"user_id": "u", "resume_id": 1})


def test_model_segments_are_bounded_lossless_and_contain_no_source_offsets():
    source = "首" + "x" * 1_600 + "尾"
    segments = _bounded_segments([{
        "id": "R" * 80, "text": source, "start": 99, "end": 1_701,
    }])

    assert "".join(item["text"] for item in segments) == source
    assert len({item["id"] for item in segments}) == len(segments)
    assert all(len(item["id"]) <= 80 and len(item["text"]) <= 800 for item in segments)
    assert all(set(item) == {"id", "text"} for item in segments)


def test_materialize_keeps_the_host_bounded_tail_segment():
    frozen = _frozen()
    tail = frozen.materials[0]["segments"][-1]
    items, _ = _materialize(_result(tail["id"]), frozen, 5)
    evidence = items[0]["evidence"][0]
    assert evidence["excerpt"].endswith("唯一尾部成果")
    assert len(evidence["excerpt"]) <= 800
    assert (evidence["source_start"], evidence["source_end"]) == (0, len(tail["text"]))


def test_materialize_rejects_missing_or_unbounded_host_refs():
    with pytest.raises(ValueError, match="invalid_evidence_ref"):
        _materialize(_result("missing"), _frozen(), 5)

    unbounded = FrozenInput("basic", 1, None, "简历", ({"kind": "resume", "hash": "h",
        "segments": [{"id": "R1", "text": "x" * 801}]},), {"user_id": "u", "resume_id": 1})
    with pytest.raises(ValueError, match="invalid_evidence_ref"):
        _materialize(_result(), unbounded, 5)


def test_basic_materialize_omits_company_category_without_poisoning_valid_questions():
    valid = _result("R1.1").model_dump(mode="json")
    company_question = {
        **valid["questions"][0],
        "text": "这家公司当前的业务重点是什么？",
        "category": "business_company",
    }
    result = GeneratedQuestionSet.model_validate({
        "questions": [company_question, valid["questions"][0]],
        "coverage": {
            **valid["coverage"],
            "covered_categories": ["business_company", "resume_deep_dive"],
        },
    })

    items, coverage = _materialize(result, _frozen(), 5)

    assert [item["category"] for item in items] == ["resume_deep_dive"]
    assert coverage["covered_categories"] == ["resume_deep_dive"]
    assert "business_company" not in coverage["omitted_categories"]
    assert coverage["policy_omissions"] == 1
    assert coverage["safety_omissions"] == 0
