"""Current immutable question-set structured-output contracts."""

import pytest
from pydantic import ValidationError

from careerdesk.features.questions.generation_models import GeneratedQuestionSet, MaterialSummary


def _question(**changes) -> dict:
    value = {
        "text": "请说明你如何处理交付风险？",
        "category": "resume_deep_dive",
        "channel": "interview",
        "response_format": "oral_text",
        "difficulty": "intermediate",
        "basis_kinds": ["resume"],
        "evidence_refs": [{"basis_kind": "resume", "ref_id": "R1"}],
        "limitations": [],
        "primary_competency": "风险管理",
        "secondary_tags": [],
        "evaluation_kind": "evidence_consistency",
        "rubric": {"essential_criteria": ["说明行动"], "quality_signals": [], "critical_errors": []},
        "answer_guide": "说明背景、行动与结果。",
        "follow_up_allowed": True,
    }
    value.update(changes)
    return value


def test_question_set_schema_is_closed_and_bounded():
    schema = GeneratedQuestionSet.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["questions"]["maxItems"] == 30
    assert GeneratedQuestionSet.model_validate({
        "questions": [_question()],
        "coverage": {"processed_sources": ["resume"], "covered_categories": ["resume_deep_dive"],
                     "omitted_categories": [], "omission_reasons": [], "limitations": []},
    }).questions[0].evidence_refs[0].ref_id == "R1"


@pytest.mark.parametrize("change", [
    {"unexpected": True},
    {"evidence_refs": [{"basis_kind": "resume", "ref_id": "R1", "start": 7}]},
    {"channel": "written", "response_format": "oral_text"},
])
def test_question_contract_rejects_drift(change):
    with pytest.raises(ValidationError):
        GeneratedQuestionSet.model_validate({
            "questions": [_question(**change)],
            "coverage": {"processed_sources": ["resume"], "covered_categories": [],
                         "omitted_categories": [], "omission_reasons": [], "limitations": []},
        })


def test_summary_points_carry_only_host_owned_source_refs():
    summary = MaterialSummary.model_validate({
        "points": [{"text": "交付风险", "refs": [
            {"basis_kind": "resume", "ref_id": "R9"},
        ]}],
        "limitations": [],
    })
    assert summary.points[0].refs[0].ref_id == "R9"
