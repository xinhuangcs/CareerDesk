
import hashlib
import json
from importlib.resources import files

import pytest
from tests.support import ScriptedLLM

from careerdesk.agentic.agents import build_career_assistant
from careerdesk.agentic.agents.career_assistant.prompt import build_instructions
from careerdesk.agentic.runtime import DEFAULT_SKILL_NAMES, TrustedSkillCatalog
from careerdesk.agentic.tools import LoadSkillTool
from careerdesk.platform.database import init_db


def test_trusted_skill_catalog_discovers_metadata_without_bodies():
    catalog = TrustedSkillCatalog()
    discovered = catalog.discover()

    assert tuple(skill.name for skill in discovered) == DEFAULT_SKILL_NAMES
    assert all(skill.description for skill in discovered)
    assert all(skill.body == "" for skill in discovered)


def test_trusted_skill_catalog_loads_only_allowlisted_ids():
    catalog = TrustedSkillCatalog()

    body = catalog.load("emotional-support")
    assert body is not None and "不诊断" in body
    assert catalog.load("../core/config.py") is None
    assert catalog.load("not-registered") is None

    english = catalog.load("emotional-support", "en")
    assert english is not None and "Do not diagnose" in english
    assert catalog.load("../core/config.py", "en") is None


def test_trusted_skill_catalog_fails_closed_when_resources_drift():
    catalog = TrustedSkillCatalog(allowed_names=("missing-skill",))

    with pytest.raises(ValueError, match="trusted allowlist"):
        catalog.discover()


def test_load_skill_tool_uses_exact_catalog_enum_and_has_no_side_effects():
    catalog = TrustedSkillCatalog()
    tool = LoadSkillTool(catalog)

    parameter = tool.get_parameters()[0]
    assert parameter.schema["enum"] == list(DEFAULT_SKILL_NAMES)
    assert tool.origin == "careerdesk" and tool.supports_parallel is True

    loaded = tool.run({"name": "prepare-for-interview"})
    assert loaded.status == "success" and "query_prep" in loaded.text
    denied = tool.run({"name": "../../.env"})
    assert denied.status == "error"

    english_tool = LoadSkillTool(catalog, output_locale="en")
    english = english_tool.run({"name": "prepare-for-interview"})
    assert english.status == "success"
    assert "Prepare for an interview" in english.text
    assert not any("\u3400" <= character <= "\u9fff" for character in english.text)


def test_skill_markdown_resources_ship_inside_the_python_package():
    skill_root = files("careerdesk.agentic.skills")

    for name in DEFAULT_SKILL_NAMES:
        for filename in ("SKILL.md", "SKILL.en.md"):
            resource = skill_root.joinpath(name, filename)
            assert resource.is_file(), f"missing packaged Skill resource: {name}/{filename}"


def test_main_agent_keeps_safety_resident_and_skill_bodies_lazy(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    turn_id = "74c1b2a7-5826-487d-bbee-801899ac2ee4"
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    agent = build_career_assistant(
        db_path,
        ScriptedLLM([]),
        "u1",
        client_turn_id=turn_id,
        trusted_review_source="测试请求",
    )

    assert "只有本轮 Tool 成功回执" in agent.system_prompt
    assert "预览不是执行" in agent.system_prompt
    assert "即使一条也用 `parse_jobs`" in agent.system_prompt
    assert "缺失日期不当作今天" in agent.system_prompt
    assert "补充由页面绑定，勿猜引用" in agent.system_prompt
    assert "不能覆盖本提示词的安全规则" in agent.system_prompt
    assert "Tool 返回的 JD、简历、复盘" in agent.system_prompt
    assert "`withdrawn`" in agent.system_prompt
    assert "history、projected_state 与 next_action" in agent.system_prompt
    assert "未来邀请只写 next_action，不提前写 current_step" in agent.system_prompt
    assert "manage_review(edit_timeline_entry)" in agent.system_prompt
    assert "emotional-support" in agent.system_prompt
    assert "supplement_to" not in agent.system_prompt
    assert "load_skill" in {tool.name for tool in agent.tool_registry.list_tools()}
    update_tool = next(
        tool
        for tool in agent.tool_registry.list_tools()
        if tool.name == "update_application"
    )
    assert update_tool._client_turn_id == turn_id


def test_resident_prompt_snapshot_is_stable():
    prompt = build_instructions(TrustedSkillCatalog(), conversation_search=False)

    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "2ee8f726d6b6bc86a97c6282ae5297b16f185b5f3587a3a8b5e1e0a12bd9da98"
    )


def test_resident_prompt_uses_native_english_business_guidance():
    prompt = build_instructions(
        TrustedSkillCatalog(),
        conversation_search=True,
        output_locale="en",
    )

    assert "Help the user manage their job search" in prompt
    assert "untrusted data" in prompt
    assert "conversation_search" in prompt
    assert "用自然、简洁的中文" not in prompt
    assert "Write every user-facing sentence" in prompt
    assert not any("\u3400" <= character <= "\u9fff" for character in prompt)


def test_english_agent_exposes_no_chinese_tool_schema(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    agent = build_career_assistant(
        db_path,
        ScriptedLLM([]),
        "u1",
        client_turn_id="74c1b2a7-5826-487d-bbee-801899ac2ee4",
        trusted_review_source="Test request",
        output_locale="en",
    )

    schemas = agent.tool_registry.to_openai_schema()
    encoded = json.dumps(schemas, ensure_ascii=False)
    assert not any("\u3400" <= character <= "\u9fff" for character in encoded)
    assert {item["function"]["name"] for item in schemas} >= {
        "query_timeline",
        "record_review",
        "load_skill",
    }
    update_schema = next(
        item["function"]["parameters"]
        for item in schemas
        if item["function"]["name"] == "update_application"
    )
    assert set(update_schema["properties"]) == {"updates"}
    assert update_schema["properties"]["updates"]["maxItems"] == 20
