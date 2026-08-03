
import asyncio
import ast
import inspect
from importlib.resources import files
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import yaml
from tests.support import ScriptedLLM

from careerdesk.agentic.agents.career_assistant.prompt import BASE_INSTRUCTIONS
from careerdesk.agentic.agents.career_assistant.toolset import build_tool_registry
from careerdesk.agentic.runtime import (
    DEFAULT_SKILL_NAMES,
    CareerDeskToolRegistry,
    TrustedSkillCatalog,
)
from careerdesk.agentic.tools import manage_jobs as manage_jobs_tools
from careerdesk.agentic.tools import manage_review as manage_review_tools
from careerdesk.agentic.tools import manage_timeline as manage_timeline_tools
from careerdesk.agentic.tools import record_review as record_review_tools
from careerdesk.platform.database import init_db, now_iso, transaction
from careerdesk.features.applications.operations.update_models import (
    ApplicationUpdateOperationDTO,
)


EXPECTED_LOCAL_TOOLS = {
    "load_skill",
    "record_review",
    "parse_jobs",
    "update_application",
    "delete_application",
    "manage_review",
    "query_timeline",
    "query_study",
    "query_library",
    "query_grill",
    "query_prep",
    "request_application_prep",
    "query_status",
    "preferences",
}
TOOL_ACTION_EFFECTS = {
    "load_skill": {"load:read"},
    "record_review": {"record:direct_operation"},
    "parse_jobs": {"parse:proposal"},
    "update_application": {
        "update:direct_operation",
        "rename_collision:proposal",
    },
    "delete_application": {"delete:proposal"},
    "manage_review": {
        "edit_timeline_entry:direct_operation",
        "undo:proposal",
    },
    "query_timeline": {"query:read"},
    "query_study": {"query:read"},
    "query_library": {"query:read"},
    "query_grill": {"query:read"},
    "query_prep": {"query:read"},
    "request_application_prep": {"request:background_command"},
    "query_status": {"query:read"},
    "preferences": {
        "list:read",
        "apply:direct_operation",
    },
}
DIRECT_OPERATION_EXECUTORS = {
    "record_review": "execute_record_operation",
    "update_application": "execute_application_update_batch",
    "manage_review": "execute_review_timeline_entry_edit_operation",
    "preferences": "execute_preference_update_operation",
}
TRUSTED_TURN_ID = "00000000-0000-4000-8000-000000000104"


def _registry(tmp_path):
    return build_tool_registry(
        str(tmp_path / "test.db"),
        ScriptedLLM([]),
        "u1",
        TrustedSkillCatalog(),
        client_turn_id=TRUSTED_TURN_ID,
        trusted_review_source="测试请求",
    )


def _completed_update(**overrides):
    state = overrides.pop("state", "completed")
    apply_result = {
        "status": "ok",
        "application_id": 1,
        "revision": 1,
        "timeline_entry_id": 1,
        "questions_updated": 0,
        "question_occurrences_updated": 0,
        "prep_invalidated": False,
    }
    result = {
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "operation_type": "application_update",
        "contract_version": 1,
        "state": state,
        "created_time": "2026-07-13T10:00:00+00:00",
        "client_turn_id": "00000000-0000-4000-8000-000000000002",
        "target": {
            "application_id": 1,
            "company": "原公司",
            "position": "原岗位",
            "application_created_time": "2026-07-13T09:00:00+00:00",
        },
        "before": {
            "company": "原公司",
            "company_id": 1,
            "position": "原岗位",
            "stage": "backlog",
            "current_step": None,
            "next_action": None,
            "paused_from_stage": None,
            "pause_reason": None,
            "application_note": None,
            "jd_text": None,
            "revision": 0,
            "application_updated_time": "2026-07-13T09:00:00+00:00",
        },
        "final": {
            "company": "原公司",
            "company_id": 1,
            "position": "原岗位",
            "stage": "offer",
            "current_step": None,
            "next_action": None,
            "paused_from_stage": None,
            "pause_reason": None,
            "application_note": None,
            "jd_text": None,
            "revision": 1,
            "application_updated_time": "2026-07-13T10:00:00+00:00",
        },
        "effect": {
            "changed_fields": [{"field": "stage", "before": "backlog", "after": "offer"}],
            "question_provenance": [],
            "question_occurrences": [],
            "prep_invalidated": False,
            "prep_restored_on_undo": False,
            "company_record_created": False,
            "company_records_retained_on_undo": True,
        },
        "result": {"apply": apply_result, "undo": None},
        "undo_available": state == "completed",
        "undo_block_reason": None if state == "completed" else "already_undone",
    }
    if state == "undone":
        result["result"]["undo"] = {**apply_result, "revision": 2}
    elif state == "stale":
        result["result"] = None
        result["undo_block_reason"] = "operation_invalid"
    result.update(overrides)
    ApplicationUpdateOperationDTO.model_validate(result)
    return result


def _completed_update_batch(*operations, no_changes=()):
    results = [
        {"index": index, "status": "completed", "operation": operation}
        for index, operation in enumerate(operations)
    ]
    results.extend(
        {
            "index": len(results),
            "status": "no_change",
            "application_id": item.get("application_id", 1),
            "company": item.get("company", "原公司"),
            "position": item.get("position", "原岗位"),
        }
        for item in no_changes
    )
    return {
        "operation_type": "application_update_batch",
        "state": "completed" if operations else "no_change",
        "requested_count": len(results),
        "changed_count": len(operations),
        "no_change_count": len(no_changes),
        "results": results,
    }


def test_local_registry_has_exact_names_and_careerdesk_origin(tmp_path):
    registry = _registry(tmp_path)
    tools = registry.list_tools()

    assert isinstance(registry, CareerDeskToolRegistry)
    assert {tool.name for tool in tools} == EXPECTED_LOCAL_TOOLS
    assert all(tool.origin == "careerdesk" for tool in tools)


def test_bulk_delete_contract_exposes_the_200_target_product_limit(tmp_path):
    tools = {tool.name: tool for tool in _registry(tmp_path).list_tools()}
    scope = next(
        parameter
        for parameter in tools["delete_application"].get_parameters()
        if parameter.name == "scope"
    )
    targets = next(
        parameter
        for parameter in tools["delete_application"].get_parameters()
        if parameter.name == "targets"
    )

    assert scope.schema == {"type": "string", "enum": ["all"]}
    assert "1–200 条" in targets.schema["description"]
    assert "最多 200 条" in BASE_INSTRUCTIONS
    assert "delete_application(scope=all)" in BASE_INSTRUCTIONS
    assert "最多 32 条" not in BASE_INSTRUCTIONS


def test_local_tool_action_effect_inventory_is_exhaustive(tmp_path):
    registered = {tool.name for tool in _registry(tmp_path).list_tools()}

    assert set(TOOL_ACTION_EFFECTS) == EXPECTED_LOCAL_TOOLS == registered
    assert all(effects for effects in TOOL_ACTION_EFFECTS.values())
    assert {
        tool_name
        for tool_name, effects in TOOL_ACTION_EFFECTS.items()
        if any(effect.endswith(":direct_operation") for effect in effects)
    } == set(DIRECT_OPERATION_EXECUTORS)
    assert {
        tool_name
        for tool_name, effects in TOOL_ACTION_EFFECTS.items()
        if any(effect.endswith(":proposal") for effect in effects)
    } == {"parse_jobs", "update_application", "delete_application", "manage_review"}


def test_direct_operation_tools_bind_host_ids_and_hide_authority_parameters(tmp_path):
    tools = {tool.name: tool for tool in _registry(tmp_path).list_tools()}
    reserved_parameters = {
        "operation_id",
        "client_turn_id",
        "confirmed",
        "approved",
        "requires_confirmation",
    }

    for tool in tools.values():
        assert reserved_parameters.isdisjoint(
            parameter.name for parameter in tool.get_parameters()
        )

    for tool_name, executor_name in DIRECT_OPERATION_EXECUTORS.items():
        tree = ast.parse(inspect.getsource(type(tools[tool_name])))
        executor_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == executor_name
        ]
        assert len(executor_calls) == 1, (
            f"{tool_name} must have exactly one {executor_name} call"
        )
        keyword_names = {keyword.arg for keyword in executor_calls[0].keywords}
        operation_keyword = "operation_ids" if tool_name == "update_application" else "operation_id"
        assert {operation_keyword, "client_turn_id"} <= keyword_names


def test_registry_parameter_errors_never_echo_root_instance_sync_or_async(tmp_path):
    sentinel = "ROOT-PRIVATE-SENTINEL"
    registry = _registry(tmp_path)

    sync, sync_executed = registry.execute_tool_checked(
        "preferences", [sentinel],
    )
    async_result, async_executed = asyncio.run(
        registry.aexecute_tool_checked("record_review", [sentinel]),
    )

    assert sync.status == async_result.status == "error"
    assert sync_executed is async_executed is False
    assert sentinel not in sync.text and sentinel not in async_result.text
    assert sync.text == async_result.text == (
        "工具参数不符合安全契约，请按参数说明修正后再调用。"
    )

    missing = registry.execute_tool("not_registered", [sentinel])
    assert missing.status == "error"
    assert "not_registered" in missing.text
    assert missing.text != sync.text


def test_registry_tool_exceptions_are_redacted_for_model_and_logs(caplog):
    sentinel = "TOOL-BODY-PRIVATE-SENTINEL"

    def sync_explode() -> str:
        raise RuntimeError(sentinel)

    async def async_explode() -> str:
        raise RuntimeError(sentinel)

    registry = CareerDeskToolRegistry()
    registry.register_callable(sync_explode)
    registry.register_callable(async_explode)
    caplog.set_level("ERROR", logger="careerdesk.agentic.runtime.tool_registry")

    sync, sync_executed = registry.execute_tool_checked("sync_explode", {})
    asynchronous, async_executed = asyncio.run(
        registry.aexecute_tool_checked("async_explode", {}),
    )

    expected = (
        "工具执行发生内部错误，未确认任何写入；"
        "请先查询当前状态再决定是否重试。"
    )
    assert sync.status == asynchronous.status == "error"
    assert sync_executed is async_executed is True
    assert sync.text == asynchronous.text == expected
    assert sentinel not in sync.text and sentinel not in asynchronous.text
    assert sentinel not in caplog.text
    assert "sync_explode" in caplog.text and "async_explode" in caplog.text
    assert caplog.text.count("RuntimeError") == 2


def test_registry_rejects_duplicate_tool_names(tmp_path):
    registry = _registry(tmp_path)
    duplicate = registry.list_tools()[0]

    with pytest.raises(ValueError, match="already registered"):
        registry.register(duplicate)


def test_each_registry_gets_a_fresh_record_review_budget(tmp_path):
    first = next(tool for tool in _registry(tmp_path).list_tools() if tool.name == "record_review")
    second = next(tool for tool in _registry(tmp_path).list_tools() if tool.name == "record_review")

    assert first is not second


@pytest.mark.parametrize("proposal_type", [
    "job_intake",
    "application_delete",
    "review_undo",
    "application_merge",
])
def test_each_high_risk_proposal_attempt_trips_shared_fence_before_prepare_or_service(
        tmp_path, monkeypatch, proposal_type):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    registry = _registry(tmp_path)
    tools = {tool.name: tool for tool in registry.list_tools()}
    observed = []

    def observe_closed_fence():
        nested = tools["delete_application"].run({"company": "不会访问数据库"})
        observed.append(nested.data)

    if proposal_type == "job_intake":
        async def fake_parse_batch(*_args, **_kwargs):
            observe_closed_fence()
            return {"status": "empty", "positions": []}

        monkeypatch.setattr(tools["parse_jobs"]._service, "parse_batch", fake_parse_batch)
        attempted = asyncio.run(tools["parse_jobs"].arun({"text": "无法解析"}))
    elif proposal_type == "application_delete":
        def fake_prepare_delete(*_args, **_kwargs):
            observe_closed_fence()
            return {"status": "not_found"}

        monkeypatch.setattr(
            manage_timeline_tools.applications,
            "prepare_application_delete_operation",
            fake_prepare_delete,
        )
        attempted = tools["delete_application"].run({"company": "不存在"})
    elif proposal_type == "review_undo":
        def fake_prepare_undo(*_args, **_kwargs):
            observe_closed_fence()
            return {"status": "not_found"}

        monkeypatch.setattr(
            manage_review_tools.reviews,
            "prepare_review_undo_operation",
            fake_prepare_undo,
        )
        attempted = tools["manage_review"].run({"action": "undo"})
    else:
        monkeypatch.setattr(
            manage_timeline_tools.applications,
            "execute_application_update_batch",
            lambda *_args, **_kwargs: {
                "operation_type": "application_update_batch",
                "state": "rejected",
                "requested_count": 1,
                "issues": [{
                    "index": 0,
                    "reason": "merge_required",
                    "detail": {
                        "source_id": 1,
                        "destination_id": 2,
                        "source_company": "源公司",
                        "source_position": "源岗位",
                        "destination_company": "目标公司",
                        "destination_position": "目标岗位",
                    },
                }],
            },
        )

        def fake_prepare_merge(*_args, **_kwargs):
            observe_closed_fence()
            return {"status": "not_found"}

        monkeypatch.setattr(
            manage_timeline_tools.applications,
            "prepare_application_merge_operation",
            fake_prepare_merge,
        )
        attempted = tools["update_application"].run({"updates": [{
            "company": "源公司", "position": "源岗位", "new_company": "目标公司",
        }]})

    assert attempted.status in {"error", "partial"}
    assert observed == [{
        "reason": "request_proposal_write_fence",
        "proposal_type": proposal_type,
    }]
    replay = tools["delete_application"].run({"company": "仍不应访问数据库"})
    assert replay.status == "error"
    assert replay.data == observed[0]


def test_proposal_attempt_blocks_five_dependency_tools_before_io_but_allows_queries_and_preferences(
        tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    registry = _registry(tmp_path)
    tools = {tool.name: tool for tool in registry.list_tools()}

    first_parse = AsyncMock(return_value={"status": "empty", "positions": []})
    monkeypatch.setattr(tools["parse_jobs"]._service, "parse_batch", first_parse)
    attempted = asyncio.run(tools["parse_jobs"].arun({"text": "无法解析"}))

    assert attempted.status == "partial"
    first_parse.assert_awaited_once()

    record_execute = AsyncMock(side_effect=AssertionError("record_review service must not run"))
    parse_batch = AsyncMock(side_effect=AssertionError("parse_jobs service must not run again"))
    monkeypatch.setattr(
        tools["record_review"]._service,
        "execute_record_operation",
        record_execute,
    )
    monkeypatch.setattr(tools["parse_jobs"]._service, "parse_batch", parse_batch)

    def forbidden_io(*_args, **_kwargs):
        raise AssertionError("fenced tool reached database/service code")

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        forbidden_io,
    )
    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "prepare_application_delete_operation",
        forbidden_io,
    )
    monkeypatch.setattr(
        manage_review_tools.reviews,
        "execute_review_timeline_entry_edit_operation",
        forbidden_io,
    )

    blocked = [
        asyncio.run(tools["record_review"].arun({"text": "不应写入"})),
        asyncio.run(tools["parse_jobs"].arun({"text": "不应解析"})),
        tools["update_application"].run({
            "updates": [{"company": "不应定位", "new_stage": "offer"}],
        }),
        tools["delete_application"].run({"company": "不应定位"}),
        tools["manage_review"].run({"action": "edit_timeline_entry"}),
    ]
    assert all(response.status == "error" for response in blocked)
    assert {response.data["reason"] for response in blocked} == {
        "request_proposal_write_fence",
    }
    assert {response.data["proposal_type"] for response in blocked} == {"job_intake"}
    record_execute.assert_not_awaited()
    parse_batch.assert_not_awaited()

    query = tools["query_timeline"].run({"action": "board"})
    saved = tools["preferences"].run({
        "action": "apply",
        "changes": [{"op": "set", "key": "目标城市", "value": "哥本哈根"}],
    })
    listed = tools["preferences"].run({"action": "list"})
    assert query.status == saved.status == listed.status == "success"
    assert query.data["total"] == 0
    assert listed.data["items"][0]["key"] == "目标城市"
    assert listed.data["items"][0]["value"] == "哥本哈根"
    assert not hasattr(tools["preferences"], "_request_proposal_write_fence")
    assert not hasattr(tools["query_timeline"], "_request_proposal_write_fence")


def test_prior_application_write_can_be_followed_by_one_proposal_attempt(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    tools = {tool.name: tool for tool in _registry(tmp_path).list_tools()}

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        lambda *_args, **_kwargs: _completed_update_batch(_completed_update()),
    )
    prepare_calls = []

    def fake_prepare_delete(*_args, **_kwargs):
        prepare_calls.append("called")
        assert tools["delete_application"]._request_proposal_write_fence.proposal_type == (
            "application_delete"
        )
        return {"status": "not_found"}

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "prepare_application_delete_operation",
        fake_prepare_delete,
    )
    written = tools["update_application"].run({"updates": [{
        "company": "原公司", "position": "原岗位", "new_stage": "offer",
    }]})
    attempted = tools["delete_application"].run({"company": "不存在"})

    assert written.status == "success"
    assert attempted.status == "error"
    assert prepare_calls == ["called"]


def test_new_registry_gets_an_open_proposal_write_fence(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    first = {tool.name: tool for tool in _registry(tmp_path).list_tools()}
    first["update_application"]._request_proposal_write_fence.trip("application_merge")

    calls = []

    def fake_prepare_delete(*_args, **_kwargs):
        calls.append("called")
        return {"status": "not_found"}

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "prepare_application_delete_operation",
        fake_prepare_delete,
    )
    second = {tool.name: tool for tool in _registry(tmp_path).list_tools()}
    result = second["delete_application"].run({"company": "任意公司"})

    assert result.status == "error"
    assert calls == ["called"]
    assert result.data is None or "reason" not in result.data


def test_direct_tools_get_independent_fences_and_update_requires_trusted_turn(tmp_path):
    db_path = str(tmp_path / "test.db")
    tools = [
        record_review_tools.RecordReviewTool(
            object(), "u1", client_turn_id=TRUSTED_TURN_ID,
        ),
        manage_jobs_tools.ParseJobsTool(object(), "u1"),
        manage_timeline_tools.UpdateApplicationTool(
            db_path, "u1", client_turn_id=TRUSTED_TURN_ID,
        ),
        manage_timeline_tools.DeleteApplicationTool(db_path, "u1"),
        manage_review_tools.ManageReviewTool(
            db_path, "u1", client_turn_id=TRUSTED_TURN_ID,
        ),
    ]

    fences = [tool._request_proposal_write_fence for tool in tools]
    assert len({id(fence) for fence in fences}) == len(tools)
    assert all(not fence.tripped and fence.proposal_type is None for fence in fences)
    assert tools[0]._client_turn_id == TRUSTED_TURN_ID
    assert tools[2]._client_turn_id == TRUSTED_TURN_ID
    assert tools[4]._client_turn_id == TRUSTED_TURN_ID
    with pytest.raises(TypeError, match="client_turn_id"):
        record_review_tools.RecordReviewTool(object(), "u1")
    with pytest.raises(TypeError, match="client_turn_id"):
        manage_timeline_tools.UpdateApplicationTool(db_path, "u1")
    with pytest.raises(TypeError, match="client_turn_id"):
        manage_review_tools.ManageReviewTool(db_path, "u1")


def test_immediate_tool_schemas_hide_trusted_ids_and_registry_propagates_turn_id(tmp_path):
    turn_id = "6f96b780-d8bf-489e-8e2d-bb718ce2ed89"
    registry = build_tool_registry(
        str(tmp_path / "test.db"),
        ScriptedLLM([]),
        "u1",
        TrustedSkillCatalog(),
        client_turn_id=turn_id,
        trusted_review_source="测试请求",
    )
    tools = {item.name: item for item in registry.list_tools()}
    tool = tools["update_application"]

    parameter_names = {parameter.name for parameter in tool.get_parameters()}
    assert parameter_names == {"updates"}
    update_schema = tool.get_parameters()[0].schema
    assert update_schema["minItems"] == 1
    assert update_schema["maxItems"] == 20
    assert update_schema["items"]["additionalProperties"] is False
    assert update_schema["items"]["required"] == ["company"]
    assert "operation_id" not in parameter_names
    assert "client_turn_id" not in parameter_names
    assert tool._client_turn_id == turn_id
    record_parameter_names = {
        parameter.name for parameter in tools["record_review"].get_parameters()
    }
    assert record_parameter_names == set()
    assert "confirmation proposals" in tools["record_review"].description
    assert all(
        "operation" not in name and "turn" not in name
        for name in record_parameter_names
    )
    assert tools["record_review"]._client_turn_id == turn_id
    assert tools["record_review"]._allow_batch is True


def test_registry_hides_trusted_review_supplement_reference_from_model_schema(tmp_path):
    reference = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    registry = build_tool_registry(
        str(tmp_path / "test.db"),
        ScriptedLLM([]),
        "u1",
        TrustedSkillCatalog(),
        client_turn_id=TRUSTED_TURN_ID,
        trusted_review_source="补充这条复盘",
        review_supplement_reference=reference,
    )
    record = next(tool for tool in registry.list_tools() if tool.name == "record_review")

    assert record.get_parameters() == []
    assert record._review_supplement_reference == reference
    assert record._allow_batch is False
    assert "supplement_to" not in record.description


def test_registry_binds_the_complete_review_source_outside_model_arguments(tmp_path):
    source = "投递甲公司后端；乙公司前端一面通过，二面安排在明天下午。"
    registry = build_tool_registry(
        str(tmp_path / "test.db"),
        ScriptedLLM([]),
        "u1",
        TrustedSkillCatalog(),
        client_turn_id=TRUSTED_TURN_ID,
        trusted_review_source=source,
    )
    record = next(tool for tool in registry.list_tools() if tool.name == "record_review")

    assert record.get_parameters() == []
    assert record._trusted_source_text == source
    schema = next(
        item for item in registry.to_openai_schema()
        if item["function"]["name"] == "record_review"
    )
    assert schema["function"]["parameters"]["properties"] == {}
    assert schema["function"]["parameters"].get("required", []) == []

    with pytest.raises(TypeError, match="trusted_review_source"):
        build_tool_registry(
            str(tmp_path / "invalid.db"),
            ScriptedLLM([]),
            "u1",
            TrustedSkillCatalog(),
            client_turn_id=TRUSTED_TURN_ID,
            trusted_review_source=None,
        )


def test_update_tool_canonicalizes_or_rejects_trusted_turn_id(tmp_path):
    turn_id = "6F96B780-D8BF-489E-8E2D-BB718CE2ED89"
    tool = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=turn_id,
    )

    assert tool._client_turn_id == turn_id.lower()
    with pytest.raises(ValueError, match="client_turn_id 必须是 UUID"):
        manage_timeline_tools.UpdateApplicationTool(
            str(tmp_path / "test.db"), "u1", client_turn_id="model-controlled-id",
        )

    with pytest.raises(ValueError, match="规范的小写 UUID"):
        record_review_tools.RecordReviewTool(
            object(), "u1", client_turn_id=turn_id,
        )
    with pytest.raises(ValueError, match="client_turn_id 必须是 UUID"):
        record_review_tools.RecordReviewTool(
            object(), "u1", client_turn_id="model-controlled-id",
        )
    with pytest.raises(ValueError, match="review_supplement_reference 必须是规范"):
        record_review_tools.RecordReviewTool(
            object(),
            "u1",
            client_turn_id=TRUSTED_TURN_ID,
            review_supplement_reference="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAA1",
        )


def test_update_tool_sends_one_canonical_batch_and_blocks_a_second_attempt(
        tmp_path, monkeypatch):
    calls = []

    def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        command = kwargs["commands"][0]
        changes = command["changes"]
        result = _completed_update(
            operation_id=str(kwargs["operation_ids"][0]),
            client_turn_id=str(kwargs["client_turn_id"]),
        )
        result["final"].update(changes)
        result["effect"]["changed_fields"] = [
            {"field": key, "before": result["before"][key], "after": value}
            for key, value in changes.items()
        ]
        return _completed_update_batch(result)

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        fake_execute,
    )
    turn_id = "560d1cb7-7779-4b81-9768-df180b20e428"
    tool = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=turn_id,
    )

    first = tool.run({"updates": [{
        "company": " 原公司 ", "position": " 原岗位 ", "new_stage": " offer ",
    }]})
    replay = tool.run({"updates": [{
        "company": "原公司", "position": "原岗位", "new_stage": "offer",
    }]})

    assert first.status == "success"
    assert replay.status == "error"
    assert "已原子修改 1 条" in first.text and "可信操作收据" in first.text
    assert "只有对应的页面撤销按钮能执行 Undo" in first.text
    assert "已经尝试过一次" in replay.text
    assert len(calls) == 1
    assert all(UUID(str(operation_id)).version == 4 for operation_id in calls[0]["operation_ids"])
    assert {str(call["client_turn_id"]) for call in calls} == {turn_id}
    assert calls[0]["commands"] == [{
        "company": "原公司",
        "position": "原岗位",
        "changes": {"stage": "offer"},
        "expected_application_id": None,
        "expected_revision": None,
    }]


def test_update_tool_can_write_jd_without_claiming_raw_text_in_receipt(tmp_path, monkeypatch):
    calls = []

    def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        changes = kwargs["commands"][0]["changes"]
        result = _completed_update()
        result["before"]["jd_text"] = None
        result["final"]["jd_text"] = changes["jd_text"]
        result["effect"]["changed_fields"] = [{
            "field": "jd_text",
            "before": None,
            "after": changes["jd_text"],
        }]
        result["effect"]["prep_invalidated"] = True
        result["result"]["apply"]["prep_invalidated"] = True
        return _completed_update_batch(result)

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        fake_execute,
    )
    tool = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"),
        "u1",
        client_turn_id=TRUSTED_TURN_ID,
    )

    result = tool.run({"updates": [{
        "company": "原公司",
        "position": "原岗位",
        "new_jd_text": "  负责 FastAPI 与 PostgreSQL  ",
    }]})

    assert result.status == "success"
    assert "已原子修改 1 条" in result.text
    assert "FastAPI" not in result.text
    assert calls[0]["commands"][0]["changes"] == {
        "jd_text": "负责 FastAPI 与 PostgreSQL",
    }


def test_update_tool_submits_multiple_roles_in_one_batch_call(tmp_path, monkeypatch):
    calls = []

    def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        operations = []
        for index, command in enumerate(kwargs["commands"], start=1):
            operation = _completed_update(
                operation_id=kwargs["operation_ids"][index - 1],
                client_turn_id=kwargs["client_turn_id"],
            )
            operation["target"]["application_id"] = index
            operation["before"]["company"] = command["company"]
            operation["before"]["position"] = command["position"]
            operation["final"]["company"] = command["company"]
            operation["final"]["position"] = command["position"]
            operations.append(operation)
        return _completed_update_batch(*operations)

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        fake_execute,
    )
    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [
        {"company": "A", "position": "P1", "new_stage": "applied"},
        {"company": "B", "position": "P2", "new_priority": "high"},
    ]})

    assert response.status == "success"
    assert len(calls) == 1
    assert len(calls[0]["operation_ids"]) == 2
    assert calls[0]["commands"] == [
        {
            "company": "A",
            "position": "P1",
            "changes": {"stage": "applied"},
            "expected_application_id": None,
            "expected_revision": None,
        },
        {
            "company": "B",
            "position": "P2",
            "changes": {"priority": "high"},
            "expected_application_id": None,
            "expected_revision": None,
        },
    ]


def test_update_tool_rejects_multi_role_identity_changes_before_io(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid identity batch must not reach the feature operation")

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        forbidden,
    )
    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [
        {"company": "A", "position": "P1", "new_company": "A2"},
        {"company": "B", "position": "P2", "new_stage": "applied"},
    ]})

    assert response.status == "error"
    assert "身份修改请作为单条请求" in response.text


def test_update_tool_writes_stage_current_step_and_structured_next_action(tmp_path):
    db_path = str(tmp_path / "state-and-plan.db")
    init_db(db_path)
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, created_time, updated_time) VALUES (?, ?, ?, ?, ?)",
            ("u1", "A", "P", timestamp, timestamp),
        )

    response = manage_timeline_tools.UpdateApplicationTool(
        db_path, "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [{
        "company": "A",
        "new_stage": "interviewing",
        "new_current_step": "一面",
        "next_stage": "interviewing",
        "next_step": "二面",
        "next_date": "2026-07-30",
        "next_time": "14:30",
        "next_note": "视频面试",
    }]})

    detail = manage_timeline_tools.applications.application_detail(db_path, "u1", 1)
    assert response.status == "success"
    assert (detail["stage"], detail["current_step"]) == ("interviewing", "一面")
    assert detail["next_action"] == {
        "stage": "interviewing",
        "step": "二面",
        "date": "2026-07-30",
        "time": "14:30",
        "note": "视频面试",
    }
    assert detail["timeline_entries"][-1]["source"] == "agent"


def test_update_tool_merges_partial_next_action_and_note_uses_revision_cas(tmp_path):
    db_path = str(tmp_path / "partial-plan.db")
    init_db(db_path)
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, next_stage, next_step, next_date, "
            "created_time, updated_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("u1", "A", "P", "interviewing", "二面", "2026-07-30", timestamp, timestamp),
        )

    plan_tool = manage_timeline_tools.UpdateApplicationTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000105",
    )
    plan_parameters = {"updates": [{
        "company": "A", "next_date": "2026-08-01", "next_time": "09:00",
    }]}
    plan_response = plan_tool.run(plan_parameters)
    plan_replay = plan_tool.run(plan_parameters.copy())
    note_response = manage_timeline_tools.UpdateApplicationTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000106",
    ).run({"updates": [{"company": "A", "append_note": "等待团队反馈"}]})
    compatible_note_response = manage_timeline_tools.UpdateApplicationTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000107",
    ).run({"updates": [{"company": "A", "new_note": "补充记录"}]})

    detail = manage_timeline_tools.applications.application_detail(db_path, "u1", 1)
    assert (
        plan_response.status
        == note_response.status
        == compatible_note_response.status
        == "success"
    )
    assert plan_replay.status == "error"
    assert "已经尝试过一次" in plan_replay.text
    assert detail["next_action"]["step"] == "二面"
    assert (detail["next_action"]["date"], detail["next_action"]["time"]) == (
        "2026-08-01", "09:00",
    )
    assert detail["application_note"] == "等待团队反馈\n补充记录"


def test_update_tool_next_patch_conflicts_and_clear_date_is_atomic(tmp_path):
    db_path = str(tmp_path / "partial-plan-clear.db")
    init_db(db_path)
    timestamp = now_iso()
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, next_stage, next_step, next_date, next_time, "
            "created_time, updated_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "u1", "A", "P", "interviewing", "二面", "2026-07-30", "14:30",
                timestamp, timestamp,
            ),
        )
    def run_once(parameters, turn_suffix):
        tool = manage_timeline_tools.UpdateApplicationTool(
            db_path,
            "u1",
            client_turn_id=f"00000000-0000-4000-8000-{turn_suffix:012d}",
        )
        return tool.run({"updates": [parameters]})

    assert run_once({
        "company": "A", "next_date": "2026-08-01", "clear_next_date": True,
    }, 105).status == "error"
    assert run_once({
        "company": "A", "next_time": "09:00", "clear_next_time": True,
    }, 106).status == "error"
    assert run_once({
        "company": "A", "next_note": "说明", "clear_next_note": True,
    }, 107).status == "error"
    cleared = run_once({"company": "A", "clear_next_date": True}, 108)

    assert cleared.status == "success"
    detail = manage_timeline_tools.applications.application_detail(db_path, "u1", 1)
    assert detail["next_action"] == {
        "stage": "interviewing", "step": "二面", "date": None,
        "time": None, "note": None,
    }


def test_no_change_batch_consumes_the_single_tool_attempt(tmp_path, monkeypatch):
    calls = []

    def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        return _completed_update_batch(no_changes=({
            "application_id": 1, "company": "原公司", "position": "原岗位",
        },))

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        fake_execute,
    )
    tool = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    )

    no_change = tool.run({"updates": [{"company": "原公司", "new_stage": "offer"}]})
    replay = tool.run({"updates": [{"company": "原公司", "new_stage": "offer"}]})

    assert no_change.status == "partial"
    assert replay.status == "error"
    assert "已经是请求的值" in no_change.text
    assert "已经尝试过一次" in replay.text
    assert len(calls) == 1


@pytest.mark.parametrize("terminal_result", [
    {
        "operation_type": "application_update_batch",
        "state": "rejected",
        "requested_count": 1,
        "issues": [{"index": 0, "reason": "not_found"}],
    },
    {
        "operation_type": "application_update_batch",
        "state": "rejected",
        "requested_count": 1,
        "issues": [{"index": 0, "reason": "ambiguous", "options": ["后端", "前端"]}],
    },
    _completed_update_batch(no_changes=({
        "application_id": 1, "company": "原公司", "position": "原岗位",
    },)),
])
def test_zero_write_terminal_outcomes_still_consume_the_single_attempt(
        tmp_path, monkeypatch, terminal_result):
    calls = []

    def fake_execute(*_args, **_kwargs):
        calls.append(True)
        return terminal_result

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        fake_execute,
    )
    tool = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    )
    parameters = {"updates": [{"company": "原公司", "new_stage": "offer"}]}

    first = tool.run(parameters)
    replay = tool.run(parameters.copy())

    assert first.status == "partial"
    assert replay.status == "error"
    assert calls == [True]


@pytest.mark.parametrize(("result", "expected_status", "text"), [
    ({"index": 0, "reason": "not_found"}, "partial", "not_found"),
    ({"index": 0, "reason": "ambiguous", "options": ["后端", "前端"]}, "partial", "ambiguous"),
    ({"index": 0, "reason": "conflict"}, "partial", "conflict"),
    ({"index": 0, "reason": "operation_undone"}, "partial", "operation_undone"),
    ({"index": 0, "reason": "operation_stale"}, "partial", "operation_stale"),
])
def test_update_tool_reports_rejected_batch_without_natural_key_preread(
        tmp_path, monkeypatch, result, expected_status, text):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy two-stage natural-key path must not run")

    monkeypatch.setattr(manage_timeline_tools.applications, "resolve_application_by_name", forbidden)
    assert not hasattr(manage_timeline_tools.applications, "record_timeline_update")
    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        lambda *_args, **_kwargs: {
            "operation_type": "application_update_batch",
            "state": "rejected",
            "requested_count": 1,
            "issues": [result],
        },
    )

    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [{"company": "原公司", "new_stage": "offer"}]})

    assert response.status == expected_status
    assert text in response.text


def test_update_tool_reports_operation_conflict_without_legacy_io(tmp_path, monkeypatch):
    def conflict(*_args, **_kwargs):
        raise manage_timeline_tools.applications.ApplicationUpdateOperationConflict(
            "operation ID 已绑定另一条命令",
        )

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        conflict,
    )
    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [{"company": "原公司", "new_stage": "offer"}]})

    assert response.status == "error"
    assert response.data == {"reason": "application_update_operation_conflict"}
    assert "另一条命令" in response.text


def test_update_tool_does_not_expose_domain_validation_details(tmp_path, monkeypatch):
    sentinel = "PRIVATE_INVALID_INPUT_SENTINEL"

    def invalid(*_args, **_kwargs):
        raise ValueError(sentinel)

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        invalid,
    )
    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [{"company": "原公司", "new_stage": "offer"}]})

    assert response.status == "error"
    assert response.text == "岗位修改参数无效；本次没有写入。"
    assert sentinel not in response.text


def test_update_tool_handles_operation_not_found_without_interrupting_agent(tmp_path, monkeypatch):
    def missing(*_args, **_kwargs):
        raise manage_timeline_tools.applications.ApplicationUpdateOperationNotFound(
            "other tenant owns operation",
        )

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        missing,
    )
    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [{"company": "原公司", "new_stage": "offer"}]})

    assert response.status == "error"
    assert response.data == {"reason": "application_update_operation_not_found"}
    assert "不存在或不属于当前用户" in response.text


def test_update_tool_rejects_any_provided_blank_change_before_io(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid command must not reach feature operation")

    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        forbidden,
    )
    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [{
        "company": "原公司", "new_stage": "offer", "new_company": "  ",
    }]})

    assert response.status == "error"
    assert "new_company 不能为空" in response.text


@pytest.mark.parametrize(("source_stage", "has_stage_note"), [
    ("backlog", True),
    ("offer", False),
])
def test_merge_with_stage_forwards_stable_ids_and_never_uses_legacy_update(
        tmp_path, monkeypatch, source_stage, has_stage_note):
    execute_calls = []
    prepare_calls = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("merge collision must not use legacy read/write")

    def fake_execute(*_args, **kwargs):
        execute_calls.append(kwargs)
        return {
            "operation_type": "application_update_batch",
            "state": "rejected",
            "requested_count": 1,
            "issues": [{
                "index": 0,
                "reason": "merge_required",
                "detail": {
                    "source_id": 11,
                    "destination_id": 22,
                    "source_company": "A",
                    "source_position": "P1",
                    "source_stage": source_stage,
                    "destination_company": "B",
                    "destination_position": "P2",
                },
            }],
        }

    def fake_prepare(*_args, **kwargs):
        prepare_calls.append(kwargs)
        return {
            "operation_type": "application_merge",
            "state": "pending",
            "source": {"application_id": 11, "company": "A", "position": "P1"},
            "destination": {"application_id": 22, "company": "B", "position": "P2"},
        }

    monkeypatch.setattr(manage_timeline_tools.applications, "resolve_application_by_name", forbidden)
    assert not hasattr(manage_timeline_tools.applications, "record_timeline_update")
    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "execute_application_update_batch",
        fake_execute,
    )
    monkeypatch.setattr(
        manage_timeline_tools.applications,
        "prepare_application_merge_operation",
        fake_prepare,
    )
    response = manage_timeline_tools.UpdateApplicationTool(
        str(tmp_path / "test.db"), "u1", client_turn_id=TRUSTED_TURN_ID,
    ).run({"updates": [{
        "company": "A",
        "position": "P1",
        "new_company": "B",
        "new_position": "P2",
        "new_stage": "offer",
    }]})

    assert response.status == "success"
    assert ("阶段改动尚未执行" in response.text) is has_stage_note
    assert execute_calls[0]["commands"][0]["changes"] == {
        "company": "B", "position": "P2", "stage": "offer",
    }
    assert prepare_calls == [{
        "source_application_id": 11,
        "source_company": "A",
        "source_position": "P1",
        "destination_application_id": 22,
        "destination_company": "B",
        "destination_position": "P2",
    }]


def test_skill_tool_references_are_valid_but_are_not_runtime_permissions(tmp_path):
    known_tools = {tool.name for tool in _registry(tmp_path).list_tools()}
    referenced: set[str] = set()
    skill_root = files("careerdesk.agentic.skills")

    for name in DEFAULT_SKILL_NAMES:
        text = skill_root.joinpath(name, "SKILL.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(text.split("---", 2)[1])
        tool_names = set(frontmatter.get("tools") or [])
        assert tool_names, f"Skill has no documented Tool references: {name}"
        assert tool_names <= known_tools, f"Skill references unknown Tools: {name}"
        referenced.update(tool_names)

    assert referenced <= known_tools - {"load_skill"}
