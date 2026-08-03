
import pytest
from pydantic import ValidationError

from careerdesk.features.applications.intake_models import (
    MAX_BATCH_JD_TEXT_CHARS,
    MAX_BATCH_POSITIONS,
    MAX_BATCH_TOTAL_TEXT_CHARS,
    MAX_CHANNEL_CHARS,
    MAX_COMPANY_CHARS,
    MAX_DEPARTMENT_CHARS,
    MAX_HIGHLIGHTS,
    MAX_HIGHLIGHT_CHARS,
    MAX_POSITION_CHARS,
    MAX_SKILLS,
    MAX_SKILL_CHARS,
    BatchParse,
    ParsedPosition,
    merge_duplicate_positions,
)


def item(**changes) -> dict:
    value = {"company": "A 公司", "position": "Agent 工程师"}
    value.update(changes)
    if isinstance(value.get("jd_text"), str):
        value.setdefault("jd_source_start", 0)
        value.setdefault("jd_source_end", len(value["jd_text"]))
    return value


def test_structured_schema_exposes_provider_compatible_hard_limits():
    schema = BatchParse.model_json_schema()
    position = schema["$defs"]["ParsedPosition"]

    assert schema["additionalProperties"] is False
    assert schema["properties"]["positions"]["maxItems"] == MAX_BATCH_POSITIONS
    assert position["additionalProperties"] is False
    assert position["properties"]["company"] == {
        "maxLength": MAX_COMPANY_CHARS,
        "minLength": 1,
        "title": "Company",
        "type": "string",
    }
    assert position["properties"]["skills"]["maxItems"] == MAX_SKILLS
    assert position["properties"]["highlights"]["maxItems"] == MAX_HIGHLIGHTS
    assert position["properties"]["applied_date"]["anyOf"][0]["pattern"] == (
        r"^\d{4}-\d{2}-\d{2}$"
    )
    assert position["properties"]["stage"]["anyOf"][0]["enum"] == [
        "backlog", "applied", "written_test", "interviewing", "offer",
        "withdrawn", "rejected", "pooled",
    ]


def test_batch_position_count_and_total_jd_budget_are_bounded():
    BatchParse.model_validate({"positions": [item()] * MAX_BATCH_POSITIONS})
    with pytest.raises(ValidationError):
        BatchParse.model_validate({"positions": [item()] * (MAX_BATCH_POSITIONS + 1)})

    half = MAX_BATCH_JD_TEXT_CHARS // 2
    BatchParse.model_validate({
        "positions": [item(company="A", jd_text="x" * half),
                      item(company="B", jd_text="y" * (MAX_BATCH_JD_TEXT_CHARS - half))],
    })
    with pytest.raises(ValidationError, match="JD 原文合计"):
        BatchParse.model_validate({
            "positions": [item(company="A", jd_text="x" * (half + 1)),
                          item(company="B", jd_text="y" * (half + 1))],
        })

    large_highlights = [
        f"{index:02d}" + "h" * (MAX_HIGHLIGHT_CHARS - 2)
        for index in range(MAX_HIGHLIGHTS)
    ]
    with pytest.raises(ValidationError, match="结构化文本合计"):
        BatchParse.model_validate({
            "positions": [
                item(company=f"C{index}", highlights=large_highlights)
                for index in range(MAX_BATCH_TOTAL_TEXT_CHARS // sum(map(len, large_highlights)) + 1)
            ],
        })


def test_required_names_trim_and_reject_blank_or_oversized_values():
    parsed = ParsedPosition(company="  A 公司  ", position="  后端工程师\n")
    assert (parsed.company, parsed.position) == ("A 公司", "后端工程师")

    for payload in (
        item(company="   "),
        item(position="\t"),
        item(company="x" * (MAX_COMPANY_CHARS + 1)),
        item(position="x" * (MAX_POSITION_CHARS + 1)),
    ):
        with pytest.raises(ValidationError):
            ParsedPosition.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"department": "x" * (MAX_DEPARTMENT_CHARS + 1)},
        {"channel": "x" * (MAX_CHANNEL_CHARS + 1)},
        {"jd_text": "x" * (MAX_BATCH_JD_TEXT_CHARS + 1)},
        {"skills": ["s"] * (MAX_SKILLS + 1)},
        {"skills": ["x" * (MAX_SKILL_CHARS + 1)]},
        {"highlights": ["h"] * (MAX_HIGHLIGHTS + 1)},
        {"highlights": ["x" * (MAX_HIGHLIGHT_CHARS + 1)]},
    ],
)
def test_optional_text_and_list_limits_are_enforced(changes):
    with pytest.raises(ValidationError):
        ParsedPosition.model_validate(item(**changes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("applied_date", "2026-2-03"),
        ("applied_date", "2026-02-30"),
        ("next_action.date", "not-a-date"),
        ("next_action.date", "2026-13-01"),
    ],
)
def test_dates_must_be_real_canonical_iso_dates(field, value):
    if field == "next_action.date":
        invalid = item(next_action={
            "stage": "interviewing", "step": "一面", "date": value,
        })
        valid = item(next_action={
            "stage": "interviewing", "step": "一面", "date": "2026-07-13",
        })
        with pytest.raises(ValidationError):
            ParsedPosition.model_validate(invalid)
        assert ParsedPosition.model_validate(valid).next_action.date == "2026-07-13"
        return
    with pytest.raises(ValidationError):
        ParsedPosition.model_validate(item(**{field: value}))
    assert ParsedPosition.model_validate(item(**{field: "2026-07-13"})).model_dump()[field] == (
        "2026-07-13"
    )


def test_explicit_stage_is_bounded_and_cannot_conflict_with_applied_date():
    assert ParsedPosition.model_validate(item(stage="applied")).stage == "applied"
    with pytest.raises(ValidationError):
        ParsedPosition.model_validate(item(stage="unknown"))
    with pytest.raises(ValidationError, match="backlog 岗位不能同时带投递日期"):
        ParsedPosition.model_validate(
            item(stage="backlog", applied_date="2026-07-13"),
        )


def test_extra_fields_are_forbidden_at_both_schema_levels():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ParsedPosition.model_validate(item(injected=True))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        BatchParse.model_validate({"positions": [item()], "injected": True})


def test_duplicate_merge_is_stable_and_keeps_first_conflicting_scalar():
    parsed = BatchParse.model_validate({
        "positions": [
            item(company=" A ", position=" 工程师 ", department=None, channel="官网",
                 stage=None, skills=["Python", "RAG"], highlights=["加分 A"]),
            item(company="A", position="工程师", department="Agent", channel="内推",
                 stage="applied", applied_date="2026-07-13", jd_text="JD 正文",
                 skills=["RAG", "SQL"], highlights=["加分 A", "加分 B"]),
            item(company="B", position="算法"),
        ],
    })

    merged = merge_duplicate_positions(parsed.positions)
    assert [(value.company, value.position) for value in merged] == [("A", "工程师"), ("B", "算法")]
    first = merged[0]
    assert first.department == "Agent"
    assert first.channel == "官网"
    assert first.stage == "applied"
    assert first.applied_date == "2026-07-13"
    assert first.jd_text == "JD 正文"
    assert first.skills == ["Python", "RAG", "SQL"]
    assert first.highlights == ["加分 A", "加分 B"]
