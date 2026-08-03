
import pytest
from pydantic import ValidationError

from careerdesk.features.resumes.ai_models import (
    MAX_KNOWLEDGE_POINT_CHARS,
    MAX_RESUME_OUTPUT_LINES,
    ResumeParse,
)


def _assert_closed_object_schemas(model) -> dict:
    schema = model.model_json_schema()
    objects = [schema, *(schema.get("$defs") or {}).values()]
    for object_schema in objects:
        if object_schema.get("type") == "object":
            assert object_schema.get("additionalProperties") is False
    return schema


def test_provider_schemas_are_closed_and_business_fields_are_required():
    parse_schema = _assert_closed_object_schemas(ResumeParse)

    assert set(parse_schema["required"]) == {"family", "lines"}
    assert set(parse_schema["$defs"]["ResumeLine"]["required"]) == {
        "line_index", "knowledge_points",
    }


def test_explicit_empty_abstention_is_valid_but_omission_and_extras_are_not():
    assert ResumeParse.model_validate({"family": "other", "lines": []}).lines == []

    for model, value in (
        (ResumeParse, {}),
        (ResumeParse, {"family": "backend", "lines": [], "secret": "x"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(value)


@pytest.mark.parametrize("family", ["", "fullstack", "BACKEND", None])
def test_resume_family_is_the_reviewed_six_value_enum(family):
    with pytest.raises(ValidationError):
        ResumeParse.model_validate({"family": family, "lines": []})


def test_resume_line_limits_and_stable_deduplication_are_enforced():
    parsed = ResumeParse.model_validate({
        "family": "backend",
        "lines": [
            {
                "line_index": 2,

                "knowledge_points": [" RAG ", "RAG"],
            },
            {
                "line_index": 2,
                "knowledge_points": ["向量检索", "重排"],
            },
        ],
    })

    assert len(parsed.lines) == 1
    assert parsed.lines[0].knowledge_points == ["RAG", "向量检索", "重排"]

    invalid_lines = [
        {"line_index": 0, "knowledge_points": []},
        {
            "line_index": 0,

            "knowledge_points": ["k" * (MAX_KNOWLEDGE_POINT_CHARS + 1)],
        },
        {
            "line_index": 0,

            "knowledge_points": ["a", "b", "c", "d"],
        },
    ]
    for line in invalid_lines:
        with pytest.raises(ValidationError):
            ResumeParse.model_validate({"family": "backend", "lines": [line]})

    repeated = {
        "line_index": 0,

        "knowledge_points": ["RAG"],
    }
    with pytest.raises(ValidationError):
        ResumeParse.model_validate({
            "family": "backend",
            "lines": [repeated] * (MAX_RESUME_OUTPUT_LINES + 1),
        })

    for coerced_index in (True, False, "0", 0.0):
        with pytest.raises(ValidationError):
            ResumeParse.model_validate({
                "family": "backend",
                "lines": [{
                    "line_index": coerced_index,

                    "knowledge_points": ["RAG"],
                }],
            })
