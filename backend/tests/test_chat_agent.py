
import asyncio
import copy
import inspect
import json
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agentmaker import LLMRequestError, Scope, ToolResponse
from tests.support import ScriptedLLM

from careerdesk.agentic.agents import build_career_assistant
from careerdesk.agentic.tools import PreferencesTool, RecordReviewTool
from careerdesk.core.config import get_settings
from careerdesk.features.applications.public import (
    ApplicationUpdateOperationDTO,
    parse_standard_workbook,
)
from careerdesk.features.preferences.operations import execute_preference_update_operation
from careerdesk.features.reviews.public import (
    ReviewService,
    approve_review_record_operation,
    list_pending_review_record_confirmations,
    list_review_record_operations_for_turn,
)
from careerdesk.orchestration.assistant import ledger as assistant_ledger
from careerdesk.orchestration.assistant.ledger import evict_expired_completed_replays
from careerdesk.orchestration.assistant.service import (
    _OutputLocaleGuardrail,
    _TOOL_STATUS_LABELS,
    _TOOL_STATUS_LABELS_EN,
    _VerifiedWriteAttestation,
    _is_direct_review_record_request,
    _proposal_surface_for_tool_result,
    _proposal_surfaces_for_tool_result,
    _tool_status_label,
    _tool_status_variant,
    _ui_actions_for_tool_result,
    _valid_application_update_batch,
    ChatTurnRejected,
    cancel_chat_turn,
    run_chat,
)
from careerdesk.orchestration.assistant.turn_cancellation import (
    TurnCancellationControl,
    register_active_turn,
    unregister_active_turn,
)
from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.platform.storage.uploads import user_upload_root


def test_ui_actions_only_accept_successful_allowlisted_local_tool_references():
    valid = ToolResponse.ok("ok", data={"ui_actions": [
        {"kind": "open_application", "resource_id": 7},
    ]})
    assert _ui_actions_for_tool_result("query_timeline", valid) == [
        {"kind": "open_application", "resource_id": 7},
    ]
    assert _ui_actions_for_tool_result("query_prep", valid) == []
    assert _ui_actions_for_tool_result(
        "query_timeline",
        ToolResponse.partial("ambiguous", data=valid.data),
    ) == []
    assert _ui_actions_for_tool_result(
        "query_timeline",
        ToolResponse.ok("bad", data={"ui_actions": [{"kind": "open_url"}]}),
    ) == []

GOLDEN_TEXT = "今天下午面了字节二面，Seed 的 LLM 应用岗。问了检查点恢复怎么保证幂等，我卡了。一周内出结果。"

GOLDEN_EXTRACTION = {
    "company": "字节",
    "position": "LLM应用",
    "channel": None,
    "history": {
        "step": "二面",
        "date": "2026-07-07",
        "outcome": None,
        "summary": "字节二面完成",
    },
    "projected_state": {"stage": "interviewing", "current_step": "二面"},
    "next_action": {
        "stage": "interviewing",
        "step": "等待面试结果",
        "date": "2026-07-14",
        "time": None,
        "note": "一周内出结果",
    },
    "questions": [{"text": "检查点恢复怎么保证幂等？", "stuck": True, "knowledge_points": ["检查点幂等"]}],
    "mood": "状态一般", "time_of_day": "afternoon", "factors": ["睡眠差"],
}

FINAL_REPLY = "已帮你记好，字节二面辛苦了！"
AGENT_TURN_ID = "00000000-0000-4000-8000-000000000101"


def test_timeline_tool_status_uses_the_product_name():
    assert _TOOL_STATUS_LABELS["query_timeline"] == "查看求职进展…"


def test_tool_status_copy_is_bilingual_and_internal_steps_stay_hidden():
    assert set(_TOOL_STATUS_LABELS) == set(_TOOL_STATUS_LABELS_EN)
    assert not any(
        "\u3400" <= character <= "\u9fff"
        for label in _TOOL_STATUS_LABELS_EN.values()
        for character in label
    )
    assert (
        _tool_status_label(
            "zh-CN", _tool_status_variant("preferences", {"action": "list"}),
        )
        == "读取偏好…"
    )
    assert (
        _tool_status_label(
            "en", _tool_status_variant("preferences", {"action": "apply"}),
        )
        == "Updating preferences…"
    )
    assert _tool_status_label("zh-CN", "load_skill") is None
    assert _tool_status_label("en", "internal_experimental_tool") is None


@pytest.mark.parametrize(
    "message",
    [
        "帮我记录一下：昨天字节后端一面通过，二面安排在周三。",
        "帮我更新一下最近的秋招进展：今天投了腾讯后端。",
        "前天荣耀云服务后端一面通过，二面是 8 月 7 日下午 2 点。",
        "今天投了爱奇艺服务端，顺丰物流平台后端已通过 HR 初筛。",
    ],
)
def test_standalone_progress_writes_use_direct_review_routing(message):
    assert _is_direct_review_record_request(message, [], None, "zh-CN") is True


@pytest.mark.parametrize(
    "message",
    [
        "确定",
        "我收到字节一面邀请，应该怎么准备？",
        "查看我投递了哪些岗位",
        "总结最近的秋招进展",
        # The record word as a noun names existing material, never a write request.
        "结合我的调研、简历和练习记录，告诉我下一场面试最该准备的 3 件事。",
        "把我的面试记录整理成一份复习清单",
        "我的复盘记录里，哪个知识点出现得最多",
        # Yes/no questions about an outcome, typed without a question mark.
        "字节那个岗挂了吗",
        "美团是不是已经被拒了",
        "腾讯那个还没被拒吧",
        "泡池子是什么意思",
        "进人才池之后一般多久有下文",
        # An intention has not happened yet.
        "打算这周把美团也投了",
    ],
)
def test_queries_and_followups_do_not_use_direct_review_routing(message):
    assert _is_direct_review_record_request(message, [], None, "zh-CN") is False


@pytest.mark.parametrize(
    "message",
    [
        "Record my job search updates: I applied to Stripe backend yesterday.",
        "I interviewed with Figma yesterday and passed the first round.",
        "Got an interview invitation from Datadog for next Tuesday.",
    ],
)
def test_english_progress_writes_use_direct_review_routing(message):
    assert _is_direct_review_record_request(message, [], None, "en") is True


@pytest.mark.parametrize(
    "message",
    [
        # A leading auxiliary opens a yes/no question even without a question mark.
        "Did I already apply to Stripe",
        "Have I interviewed with Figma yet",
        "Was I rejected by Datadog",
        "Is the Stripe application still open",
        "Tell me the three things to prepare, using my research and practice records",
        # An intention has not happened yet.
        "I am thinking about applying to Stripe next week",
    ],
)
def test_english_queries_do_not_use_direct_review_routing(message):
    assert _is_direct_review_record_request(message, [], None, "en") is False


def test_user_cancel_stops_an_active_model_turn_without_recovery(db_path):
    started = asyncio.Event()
    turn_id = str(uuid4())

    class Agent:
        async def astream_run(self, _payload, *, scope):
            started.set()
            await asyncio.Event().wait()
            yield "unreachable"

    async def scenario():
        collecting = asyncio.create_task(_collect_chat(
            "停止这轮",
            str(uuid4()),
            turn_id,
            agent_factory=lambda _hooks: Agent(),
        ))
        await started.wait()
        status = cancel_chat_turn("u1", turn_id)
        events = await collecting
        return status, events

    status, events = run(scenario())
    assert status.state == "running" and status.terminal is False
    assert [event.event for event in events] == ["error"]
    assert events[0].data == {
        "code": "turn_cancelled",
        "message": "本轮已取消，未确认方案已丢弃。",
        "retryable": False,
        "history_committed": False,
        "attachments": "retained",
        "client_turn_id": turn_id,
    }
    assert assistant_ledger.read_turn_state(db_path, "u1", turn_id) == "cancelled"


def test_user_cancel_rejects_review_proposals_from_the_stopped_turn(db_path):
    source = GOLDEN_TEXT
    turn_id = str(uuid4())
    proposal_ready = asyncio.Event()
    review_service = ReviewService(
        db_path,
        ScriptedLLM(batch_script(single_item_batch(source, golden()))),
    )
    tool = RecordReviewTool(
        review_service,
        "u1",
        client_turn_id=turn_id,
        allow_batch=True,
    )

    class Agent:
        def __init__(self, hooks):
            self._hooks = hooks

        async def astream_run(self, _payload, *, scope):
            for hook in self._hooks:
                await _maybe_await(hook.before_tool("record_review", {"text": source}))
            result = await tool.arun({"text": source})
            for hook in self._hooks:
                await _maybe_await(hook.after_tool(
                    "record_review",
                    {"text": source},
                    result,
                ))
            proposal_ready.set()
            await asyncio.Event().wait()
            yield "unreachable"

    async def scenario():
        collecting = asyncio.create_task(_collect_chat(
            source,
            str(uuid4()),
            turn_id,
            agent_factory=lambda hooks: Agent(hooks),
        ))
        await asyncio.wait_for(proposal_ready.wait(), timeout=2)
        cancel_chat_turn("u1", turn_id)
        return await collecting

    events = run(scenario())
    assert events[-1].data["code"] == "turn_cancelled"
    operations = list_review_record_operations_for_turn(db_path, "u1", turn_id)
    assert len(operations) == 1 and operations[0]["state"] == "rejected"
    assert list_pending_review_record_confirmations(db_path, "u1") == []


def test_user_cancel_wins_after_model_exhaustion_before_turn_commit(db_path):
    turn_id = str(uuid4())

    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "这是一段已经生成完成但尚未提交的回答。"

    async def scenario():
        events = []
        status = None
        async for event in run_chat(
            "停止提交",
            str(uuid4()),
            "u1",
            client_turn_id=turn_id,
            agent_factory=lambda _hooks: Agent(),
        ):
            events.append(event)
            if event.event == "message_delta":
                status = cancel_chat_turn("u1", turn_id)
        return status, events

    status, events = run(scenario())
    assert status is not None and status.state == "running"
    assert events[-1].event == "error"
    assert events[-1].data["code"] == "turn_cancelled"
    assert not any(event.event == "done" for event in events)
    assert assistant_ledger.read_turn_state(db_path, "u1", turn_id) == "cancelled"


def test_user_cancel_discards_deferred_agent_history(db_path):
    turn_id = str(uuid4())
    session_id = str(uuid4())

    def factory(hooks):
        return build_career_assistant(
            db_path,
            ScriptedLLM(["这段回答不应进入后续对话历史。"]),
            "u1",
            client_turn_id=turn_id,
            trusted_review_source="这条消息也不应进入后续对话历史",
            hooks=hooks,
        )

    async def scenario():
        events = []
        async for event in run_chat(
            "这条消息也不应进入后续对话历史",
            session_id,
            "u1",
            client_turn_id=turn_id,
            agent_factory=factory,
        ):
            events.append(event)
            if event.event == "message_delta":
                cancel_chat_turn("u1", turn_id)
        return events

    events = run(scenario())
    assert events[-1].data["code"] == "turn_cancelled"
    assert count(
        db_path,
        "SELECT COUNT(*) FROM session_messages WHERE sc_user=? AND sc_session=?",
        "u1",
        session_id,
    ) == 0


def test_user_cancel_preserves_a_completed_tool_receipt_as_unknown(db_path):
    turn_id = str(uuid4())
    allow_tool_finish = asyncio.Event()

    def factory(hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope):
                parameters = {
                    "updates": [{"company": "测试公司", "new_stage": "offer"}],
                }
                hooks[0].before_tool("update_application", parameters)
                await allow_tool_finish.wait()
                hooks[0].after_tool(
                    "update_application",
                    parameters,
                    ToolResponse.ok(
                        "ok",
                        data=completed_application_update_batch(
                            "测试公司", "测试岗位", "offer",
                            client_turn_id=turn_id,
                        ),
                    ),
                )
                yield "unreachable"

        return Agent()

    async def scenario():
        events = []
        async for event in run_chat(
            "更新岗位",
            str(uuid4()),
            "u1",
            client_turn_id=turn_id,
            agent_factory=factory,
        ):
            events.append(event)
            if event.event == "tool_status":
                cancel_chat_turn("u1", turn_id)
                allow_tool_finish.set()
        return events

    events = run(scenario())
    assert events[-1].event == "error"
    assert events[-1].data["code"] == "turn_outcome_unknown"
    assert assistant_ledger.read_turn_state(db_path, "u1", turn_id) == "unknown"


def test_user_cancel_never_claims_an_unregistered_running_turn_was_cancelled(db_path):
    turn_id = str(uuid4())
    decision = assistant_ledger.claim_turn(
        db_path,
        "u1",
        turn_id,
        str(uuid4()),
        "a" * 64,
    )
    assert decision.status == "execute"

    with pytest.raises(ChatTurnRejected) as rejected:
        cancel_chat_turn("u1", turn_id)

    assert rejected.value.data["code"] == "turn_in_progress"
    assert rejected.value.retry_after == 1
    assert assistant_ledger.read_turn_state(db_path, "u1", turn_id) == "running"


def test_user_cancel_retries_when_an_absent_turn_is_claimed_during_fencing(
    db_path,
    monkeypatch,
):
    from careerdesk.orchestration.assistant import service as assistant_service

    turn_id = str(uuid4())
    session_id = str(uuid4())
    cancellation = TurnCancellationControl()
    original_cancel_if_absent = assistant_service.cancel_chat_turn_if_absent
    calls = 0

    def raced_cancel_if_absent(user_id: str, client_turn_id: str):
        claimed = assistant_ledger.claim_turn(
            db_path,
            user_id,
            client_turn_id,
            session_id,
            "a" * 64,
        )
        assert claimed.status == "execute"
        register_active_turn(db_path, user_id, client_turn_id, cancellation)
        return original_cancel_if_absent(user_id, client_turn_id)

    def request_cancel(db: str, user_id: str, client_turn_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "missing"
        assert db == db_path
        return "accepted" if cancellation.request() else "finalizing"

    monkeypatch.setattr(assistant_service, "cancel_chat_turn_if_absent", raced_cancel_if_absent)
    monkeypatch.setattr(assistant_service, "request_active_turn_cancel", request_cancel)
    try:
        status = cancel_chat_turn("u1", turn_id)
    finally:
        unregister_active_turn(db_path, "u1", turn_id, cancellation)

    assert calls == 2
    assert cancellation.requested is True
    assert status.state == "running" and status.terminal is False


def test_user_cancel_returns_a_terminal_turn_that_finished_before_registry_lookup(
    db_path,
):
    turn_id = str(uuid4())
    session_id = str(uuid4())
    claimed = assistant_ledger.claim_turn(
        db_path,
        "u1",
        turn_id,
        session_id,
        "a" * 64,
    )
    assistant_ledger.complete_turn(
        db_path,
        "u1",
        turn_id,
        claimed.attempt_token,
        [
            {"event": "message_snapshot", "data": {"text": "done"}},
            {
                "event": "done",
                "data": {
                    "session": session_id,
                    "message_id": str(uuid4()),
                    "client_turn_id": turn_id,
                    "history_committed": True,
                    "attachments": "consumed",
                    "replayed": True,
                },
            },
        ],
    )

    status = cancel_chat_turn("u1", turn_id)

    assert status.state == "completed" and status.terminal is True


def test_turn_commit_boundary_rejects_late_cancellation():
    cancellation = TurnCancellationControl()

    cancellation.begin_commit()

    assert cancellation.request() is False
    assert cancellation.requested is False


def test_turn_commit_boundary_reports_finalizing(db_path):
    turn_id = str(uuid4())
    decision = assistant_ledger.claim_turn(
        db_path,
        "u1",
        turn_id,
        str(uuid4()),
        "a" * 64,
    )
    assert decision.status == "execute"
    cancellation = TurnCancellationControl()
    cancellation.begin_commit()
    register_active_turn(db_path, "u1", turn_id, cancellation)
    try:
        with pytest.raises(ChatTurnRejected) as rejected:
            cancel_chat_turn("u1", turn_id)
    finally:
        unregister_active_turn(db_path, "u1", turn_id, cancellation)

    assert rejected.value.data["code"] == "turn_finalizing"
    assert rejected.value.retry_after == 1
    assert assistant_ledger.read_turn_state(db_path, "u1", turn_id) == "running"


def record_tool(
    service,
    sequence: int,
    *,
    review_supplement_reference: str | None = None,
    trusted_source_text: str | None = None,
    allow_batch: bool = False,
) -> RecordReviewTool:
    return RecordReviewTool(
        service,
        "u1",
        client_turn_id=f"00000000-0000-4000-8000-{sequence:012d}",
        review_supplement_reference=review_supplement_reference,
        trusted_source_text=trusted_source_text,
        allow_batch=allow_batch,
    )


def golden(**overrides) -> dict:
    extraction = copy.deepcopy(GOLDEN_EXTRACTION)
    history_updates = {
        field: overrides.pop(key)
        for key, field in (
            ("history_step", "step"),
            ("history_date", "date"),
            ("history_outcome", "outcome"),
            ("history_summary", "summary"),
        )
        if key in overrides
    }
    if history_updates:
        extraction["history"].update(history_updates)
    projected_updates = {
        field: overrides.pop(key)
        for key, field in (
            ("projected_stage", "stage"),
            ("projected_step", "current_step"),
        )
        if key in overrides
    }
    if projected_updates:
        extraction["projected_state"].update(projected_updates)
    extraction.update(overrides)
    return extraction


def single_item_batch(source_text: str, extraction: dict) -> dict:
    """Return a compact test description for one batch item."""
    return {"items": [{"source_text": source_text, "extraction": extraction}]}


def batch_script(batch: dict) -> list[str]:
    """Encode the identity-manifest call followed by targeted item calls."""
    manifest = {"items": [
        {
            "source_text": item["source_text"],
            "company": item["extraction"]["company"],
            "position": item["extraction"]["position"],
        }
        for item in batch["items"]
    ]}
    return [
        json.dumps(manifest, ensure_ascii=False),
        *(
            json.dumps(item["extraction"], ensure_ascii=False)
            for item in batch["items"]
        ),
    ]


def completed_application_update_batch(
    company: str,
    position: str,
    stage: str,
    *,
    client_turn_id: str = "00000000-0000-4000-8000-000000000032",
) -> dict:
    before_stage = "backlog" if stage != "backlog" else "applied"
    operation = {
        "operation_id": "00000000-0000-4000-8000-000000000031",
        "operation_type": "application_update",
        "contract_version": 1,
        "state": "completed",
        "created_time": "2026-07-13T10:00:00+00:00",
        "client_turn_id": client_turn_id,
        "target": {
            "application_id": 1,
            "company": company,
            "position": position,
            "application_created_time": "2026-07-13T09:00:00+00:00",
        },
        "before": {
            "company": company,
            "position": position,
            "stage": before_stage,
            "revision": 0,
            "application_updated_time": "2026-07-13T09:00:00+00:00",
        },
        "final": {
            "company": company,
            "position": position,
            "stage": stage,
            "revision": 1,
            "application_updated_time": "2026-07-13T10:00:00+00:00",
        },
        "effect": {
            "changed_fields": [{
                "field": "stage", "before": before_stage, "after": stage,
            }],
            "question_provenance": [],
            "question_occurrences": [],
            "prep_invalidated": False,
            "prep_restored_on_undo": False,
            "company_record_created": False,
            "company_records_retained_on_undo": True,
        },
        "result": {
            "apply": {
                "status": "ok",
                "application_id": 1,
                "revision": 1,
                "timeline_entry_id": 1,
                "questions_updated": 0,
                "question_occurrences_updated": 0,
                "prep_invalidated": False,
            },
            "undo": None,
        },
        "undo_available": True,
        "undo_block_reason": None,
    }
    validated = ApplicationUpdateOperationDTO.model_validate(operation).model_dump()
    return {
        "operation_type": "application_update_batch",
        "state": "completed",
        "requested_count": 1,
        "changed_count": 1,
        "no_change_count": 0,
        "results": [{
            "index": 0,
            "status": "completed",
            "operation": validated,
        }],
    }


def run(coroutine):
    return asyncio.run(coroutine)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        await value


async def _collect_chat(message, session_id, turn_id, *, agent_factory):
    return [event async for event in run_chat(
        message,
        session_id,
        "u1",
        client_turn_id=turn_id,
        agent_factory=agent_factory,
    )]


def rendered_chat_text(events) -> str:
    text = ""
    for event in events:
        if event.event == "message_delta":
            text += event.data["text"]
        elif event.event == "message_snapshot":
            text = event.data["text"]
    return text


def count(db_path: str, sql: str, *params) -> int:
    with read_connection(db_path) as conn:
        (value,) = conn.execute(sql, params).fetchone()
    return value


def test_proposal_surface_only_appears_after_this_tool_created_a_pending_operation():
    operation_id = "00000000-0000-4000-8000-000000000010"
    assert _proposal_surface_for_tool_result(
        "parse_jobs",
        ToolResponse.ok("ok", data={"status": "preview", "operation_id": operation_id}),
    ) == ("intake", operation_id)
    assert _proposal_surface_for_tool_result(
        "update_application",
        ToolResponse.ok("ok", data={
            "operation_type": "application_merge",
            "state": "pending",
            "operation_id": operation_id,
        }),
    ) == ("application_merge", operation_id)
    assert _proposal_surface_for_tool_result(
        "update_application",
        ToolResponse.ok(
            "ok",
            data=completed_application_update_batch("测试公司", "测试岗位", "offer"),
        ),
    ) is None


def test_delete_batch_reveals_every_exact_pending_proposal_surface():
    operation_ids = [
        f"00000000-0000-4000-8000-{index:012d}"
        for index in range(1, 201)
    ]
    result = ToolResponse.ok("ok", data={
        "operation_type": "application_delete_batch",
        "state": "pending",
        "operations": [
            {
                "operation_type": "application_delete",
                "state": "pending",
                "operation_id": operation_id,
            }
            for operation_id in operation_ids
        ],
    })

    assert _proposal_surfaces_for_tool_result("delete_application", result) == [
        ("application_delete", operation_id) for operation_id in operation_ids
    ]
    assert _proposal_surface_for_tool_result("delete_application", result) is None
    assert _proposal_surface_for_tool_result(
        "delete_application",
        ToolResponse.error("failed", data={
            "operation_type": "application_delete",
            "state": "pending",
        }),
    ) is None


def test_stage_correction_requires_a_matching_trusted_update_receipt():
    attestation = _VerifiedWriteAttestation(
        "把香港浸会大学的申请改回笔试阶段",
        (("香港浸会大学", "副教授"), ("香港理工大学", "助教")),
    )
    unsupported = attestation.check("已更新，香港浸会大学已回到笔试阶段。")
    assert unsupported.passed is False
    assert "没有取得" in unsupported.message

    attestation.observe_tool(
        "update_application",
        {"updates": [{"company": "香港理工大学", "new_stage": "written_test"}]},
        ToolResponse.ok(
            "ok",
            data=completed_application_update_batch(
                "香港理工大学", "助教", "written_test",
            ),
        ),
    )
    assert attestation.check("已更新。").passed is False

    attestation.observe_tool(
        "update_application",
        {"updates": [{"company": "香港浸会大学", "new_stage": "written_test"}]},
        ToolResponse.ok(
            "ok",
            data=completed_application_update_batch(
                "香港浸会大学", "副教授", "written_test",
            ),
        ),
    )
    assert attestation.check("已更新，香港浸会大学已回到笔试阶段。").passed is True


def test_batch_stage_attestation_requires_every_named_application_receipt():
    attestation = _VerifiedWriteAttestation(
        "把 A 公司后端和 B 公司前端都改回笔试阶段",
        (("A 公司", "后端"), ("B 公司", "前端")),
    )
    attestation.observe_tool(
        "update_application",
        {"updates": [{
            "company": "A 公司", "position": "后端", "new_stage": "written_test",
        }]},
        ToolResponse.ok(
            "ok",
            data=completed_application_update_batch(
                "A 公司", "后端", "written_test",
            ),
        ),
    )

    assert attestation.check("已更新，两个岗位都回到笔试阶段。").passed is False


def test_unverified_generic_write_claim_is_rejected_without_a_correction_request():
    attestation = _VerifiedWriteAttestation("你好")
    assert attestation.check("已更新，记录保存好了。").passed is False
    assert attestation.check("我已经更新了这个岗位。").passed is False
    assert attestation.check("我可以帮你更新，需要告诉我目标岗位。").passed is True
    assert attestation.check("已更新的简历共有两份。").passed is True

    english = _VerifiedWriteAttestation("Hello", output_locale="en")
    rejected = english.check("I have updated the application.")
    assert rejected.passed is False
    assert "trusted receipt" in rejected.message
    assert english.check("I’ve updated the application.").passed is False
    assert english.check("Your saved résumé has two versions.").passed is True


def test_write_receipt_only_attests_supported_completion_claims():
    attestation = _VerifiedWriteAttestation("更新岗位")
    attestation.observe_tool(
        "update_application",
        {"updates": [{"company": "测试公司", "new_stage": "offer"}]},
        ToolResponse.ok(
            "ok",
            data=completed_application_update_batch("测试公司", "测试岗位", "offer"),
        ),
    )

    assert attestation.check("已更新了这个岗位。").passed is True
    assert attestation.check("已删除了这个岗位。").passed is False
    assert attestation.check("已合并了两个岗位。").passed is False

    preference = _VerifiedWriteAttestation("保存偏好")
    preference.observe_tool(
        "preferences",
        {"action": "apply"},
        ToolResponse.ok("ok", data={
            "operation_type": "preference_update",
            "effects": [{"action": "set", "key": "city", "outcome": "updated"}],
            "result": {"changed_count": 1},
        }),
    )
    assert preference.check("已更新求职偏好。").passed is True
    assert preference.check("已删除了这个岗位。").passed is False


def test_malformed_batch_receipt_cannot_attest_an_application_write():
    attestation = _VerifiedWriteAttestation("更新两个岗位")
    attestation.observe_tool(
        "update_application",
        {"updates": [
            {"company": "A", "new_stage": "applied"},
            {"company": "B", "new_stage": "offer"},
        ]},
        ToolResponse.ok("ok", data={
            "operation_type": "application_update_batch",
            "state": "completed",
            "requested_count": 2,
            "changed_count": 2,
            "no_change_count": 0,
            "results": [{
                "index": 0,
                "status": "completed",
                "operation": {
                    "operation_type": "application_update",
                    "state": "completed",
                },
            }],
        }),
    )

    assert attestation.check("已更新两个岗位。").passed is False


def test_application_update_batch_receipt_validation_is_strict():
    valid = completed_application_update_batch("A", "P", "offer")
    assert _valid_application_update_batch(valid) is True
    assert _valid_application_update_batch(
        valid,
        "00000000-0000-4000-8000-000000000032",
    ) is True
    assert _valid_application_update_batch(
        valid,
        "00000000-0000-4000-8000-000000000099",
    ) is False

    for field in ("requested_count", "changed_count", "no_change_count"):
        malformed = copy.deepcopy(valid)
        malformed[field] = True
        assert _valid_application_update_batch(malformed) is False

    malformed = copy.deepcopy(valid)
    malformed["results"][0]["index"] = False
    assert _valid_application_update_batch(malformed) is False

    malformed = copy.deepcopy(valid)
    malformed["results"][0]["operation"].pop("operation_id")
    assert _valid_application_update_batch(malformed) is False

    no_change = {
        "operation_type": "application_update_batch",
        "state": "no_change",
        "requested_count": 1,
        "changed_count": 0,
        "no_change_count": 1,
        "results": [{
            "index": 0,
            "status": "no_change",
            "application_id": 1,
            "company": "A",
            "position": "P",
        }],
    }
    assert _valid_application_update_batch(no_change) is True
    no_change["results"][0]["application_id"] = True
    assert _valid_application_update_batch(no_change) is False


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_LLM_MODEL", "")
    get_settings.cache_clear()
    path = get_settings().db_path
    init_db(path)
    yield path
    get_settings.cache_clear()


def test_record_review_tool_success(db_path):
    service = ReviewService(db_path, ScriptedLLM([
        json.dumps(GOLDEN_EXTRACTION, ensure_ascii=False),
    ]))
    tool = record_tool(service, 1)
    response = run(tool.arun({"text": GOLDEN_TEXT}))
    assert response.status == "partial"
    assert "复盘方案" in response.text and "统一确认" in response.text
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    approved = approve_review_record_operation(
        db_path,
        "u1",
        response.data["operation_id"],
    )
    assert approved["state"] == "completed" and approved["outcome"] == "applied"
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1


def test_record_review_tool_splits_two_jobs_into_independent_proposals(db_path):
    message = "我昨天面试了百度的agent开发，投递了重庆理工大学的助理教师岗位"
    batch = {
        "items": [
            {
                "source_text": "我昨天面试了百度的agent开发",
                "extraction": golden(
                    company="百度", position="agent开发",
                    history_step="一面", history_date="2026-07-16",
                    history_summary="百度 agent 开发一面完成",
                    projected_stage="interviewing", projected_step="一面",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
            {
                "source_text": "投递了重庆理工大学的助理教师岗位",
                "extraction": golden(
                    company="重庆理工大学", position="助理教师",
                    history_step="投递", history_date="2026-07-17",
                    history_summary="投递助理教师岗位",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
        ],
    }
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))
    tool = record_tool(
        service,
        20,
        trusted_source_text=message,
        allow_batch=True,
    )
    assert tool.get_parameters() == []
    response = run(tool.arun({}))

    assert response.status == "partial"
    assert "拆成 2 条独立复盘方案" in response.text
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert len(pending) == 2
    by_company = {
        operation["preview"]["extraction"]["company"]: operation
        for operation in pending
    }
    assert by_company["百度"]["preview"]["extraction"]["history"]["summary"] == (
        "百度 agent 开发一面完成"
    )
    assert by_company["重庆理工大学"]["preview"]["extraction"]["history"]["summary"] == (
        "投递助理教师岗位"
    )
    duplicate = run(tool.arun({}))
    assert duplicate.status == "error"
    assert duplicate.data == {"reason": "single_write_budget_exhausted"}

    for operation in pending:
        approve_review_record_operation(db_path, "u1", operation["operation_id"])
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT company, position, stage FROM applications ORDER BY company",
        ).fetchall() == [
            ("百度", "agent开发", "interviewing"),
            ("重庆理工大学", "助理教师", "applied"),
        ]
        assert conn.execute(
            """
            SELECT timeline_entries.summary
            FROM timeline_entries
            JOIN applications ON applications.id = timeline_entries.application_id
            ORDER BY applications.company
            """,
        ).fetchall() == [
            ("百度 agent 开发一面完成",),
            ("投递助理教师岗位",),
        ]


def test_explicit_multi_role_progress_bypasses_main_agent_and_creates_cards(
    db_path,
    monkeypatch,
):
    from careerdesk.orchestration.assistant import service as assistant_module
    from careerdesk.platform.ai import client as llm_client

    message = "我昨天面试了百度的agent开发，投递了重庆理工大学的助理教师岗位"
    batch = {
        "items": [
            {
                "source_text": "我昨天面试了百度的agent开发",
                "extraction": golden(
                    company="百度", position="agent开发",
                    history_step="一面", history_date="2026-08-01",
                    history_summary="百度 agent 开发一面完成",
                    projected_stage="interviewing", projected_step="一面",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
            {
                "source_text": "投递了重庆理工大学的助理教师岗位",
                "extraction": golden(
                    company="重庆理工大学", position="助理教师",
                    history_step="投递", history_date="2026-08-02",
                    history_summary="投递助理教师岗位",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
        ],
    }
    llm = ScriptedLLM(batch_script(batch))
    settings = SimpleNamespace(
        db_path=db_path,
        llm_model="openai:test",
        llm_context_window=1_000_000,
        llm_max_output_tokens=32_768,
        strict_offline=False,
        conversation_embedding_enabled=False,
    )
    monkeypatch.setattr(assistant_module, "get_settings", lambda: settings)
    monkeypatch.setattr(llm_client, "build_llm", lambda *_args, **_kwargs: llm)
    monkeypatch.setattr(
        "careerdesk.agentic.agents.build_career_assistant",
        lambda *_args, **_kwargs: pytest.fail("main Agent must not route progress writes"),
    )

    async def collect():
        return [event async for event in run_chat(
            message,
            "direct-review-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000420",
        )]

    events = run(collect())

    assert [event.event for event in events] == [
        "tool_status", "message_delta", "done",
    ]
    assert events[0].data["tool"] == "record_review"
    assert "拆成 2 条独立复盘方案" in events[1].data["text"]
    assert len(list_pending_review_record_confirmations(db_path, "u1")) == 2
    assert llm.calls == 3
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0


def test_direct_review_model_failure_completes_without_unknown_turn(
    db_path,
    monkeypatch,
):
    from agentmaker import LLMRequestError
    from careerdesk.orchestration.assistant import service as assistant_module
    from careerdesk.platform.ai import client as llm_client

    class FailingLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            raise LLMRequestError("provider unavailable")

    settings = SimpleNamespace(
        db_path=db_path,
        llm_model="openai:test",
        llm_context_window=1_000_000,
        llm_max_output_tokens=32_768,
        strict_offline=False,
        conversation_embedding_enabled=False,
    )
    monkeypatch.setattr(assistant_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        llm_client,
        "build_llm",
        lambda *_args, **_kwargs: FailingLLM(),
    )
    turn_id = "00000000-0000-4000-8000-000000000421"

    async def collect():
        return [event async for event in run_chat(
            "帮我记录一下：昨天字节后端一面通过。",
            "direct-review-failure-session",
            "u1",
            client_turn_id=turn_id,
        )]

    events = run(collect())

    assert [event.event for event in events] == [
        "tool_status", "message_delta", "done",
    ]
    assert "没有生成或写入岗位方案" in events[1].data["text"]
    assert assistant_ledger.read_turn_state(db_path, "u1", turn_id) == "completed"
    assert list_review_record_operations_for_turn(db_path, "u1", turn_id) == []


def test_record_review_tool_splits_shared_predicates_into_four_independent_jobs(db_path):
    message = (
        "我昨天投递了上海交大的助理教授岗位，以及清华大学的教授岗位。"
        "同时通过了香港浸会大学助理的二面，并拿到了香港大学教授的offer"
    )
    batch = {
        "items": [
            {
                "source_text": "我昨天投递了上海交大的助理教授岗位",
                "extraction": golden(
                    company="上海交大", position="助理教授",
                    history_step="投递", history_date="2026-07-17",
                    history_summary="投递上海交大助理教授",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
            {
                # Providers sometimes complete the shared predicate. The service must
                # safely re-anchor this rewritten sentence to the exact identity span.
                "source_text": "我昨天投递了清华大学的教授岗位",
                "extraction": golden(
                    company="清华大学", position="教授",
                    history_step="投递", history_date="2026-07-17",
                    history_summary="投递清华大学教授",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
            {
                "source_text": "同时通过了香港浸会大学助理的二面",
                "extraction": golden(
                    company="香港浸会大学", position="助理",
                    history_step="二面", history_date="2026-07-17",
                    history_outcome="passed",
                    history_summary="香港浸会大学助理二面通过",
                    projected_stage="interviewing", projected_step="二面",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
            {
                "source_text": "并拿到了香港大学教授的offer",
                "extraction": golden(
                    company="香港大学", position="教授",
                    history_step="收到 Offer", history_date="2026-07-17",
                    history_outcome="passed",
                    history_summary="拿到香港大学教授 Offer",
                    projected_stage="offer", projected_step="收到 Offer",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
        ],
    }
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 22, allow_batch=True).arun({"text": message}))

    assert response.status == "partial"
    assert "拆成 4 条独立复盘方案" in response.text
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert len(pending) == 4
    assert {
        (item["preview"]["extraction"]["company"], item["preview"]["extraction"]["position"])
        for item in pending
    } == {
        ("上海交大", "助理教授"),
        ("清华大学", "教授"),
        ("香港浸会大学", "助理"),
        ("香港大学", "教授"),
    }
    with read_connection(db_path) as conn:
        sources = {
                row[0]
                for row in conn.execute(
                    "SELECT content FROM journal WHERE kind='review'",
                ).fetchall()
        }
    assert "清华大学的教授" in sources
    assert "我昨天投递了清华大学的教授岗位" not in sources

    for operation in pending:
        approve_review_record_operation(db_path, "u1", operation["operation_id"])
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT company, position, stage FROM applications ORDER BY company, position",
        ).fetchall() == [
            ("上海交大", "助理教授", "applied"),
            ("清华大学", "教授", "applied"),
            ("香港大学", "教授", "offer"),
            ("香港浸会大学", "助理", "interviewing"),
        ]


def test_realistic_five_role_update_creates_exactly_five_proposals(db_path):
    message = (
        "前天荣耀云服务后端一面通过，二面是 8 月 7 日下午 2 点；"
        "今天投了爱奇艺视频平台服务端和中兴通讯云计算软件开发。"
        "金山办公协同后端明晚 7 点在线测评，顺丰科技物流平台后端已通过 "
        "HR 初筛，等待一面安排。"
    )
    batch = {"items": [
        {
            "source_text": "荣耀云服务后端",
            "extraction": golden(
                company="荣耀", position="云服务后端", history_step="一面",
                history_date="2026-08-01", history_outcome="passed",
                history_summary="荣耀云服务后端一面通过",
                projected_stage="interviewing", projected_step="一面",
                next_action={
                    "stage": "interviewing", "step": "二面", "date": "2026-08-07",
                    "time": "14:00", "note": None,
                },
                questions=[], mood=None, time_of_day=None, factors=[],
            ),
        },
        {
            "source_text": "爱奇艺视频平台服务端",
            "extraction": golden(
                company="爱奇艺", position="视频平台服务端", history_step="投递",
                history_date="2026-08-03", history_summary="投递爱奇艺视频平台服务端",
                projected_stage="applied", projected_step="完成投递", next_action=None,
                questions=[], mood=None, time_of_day=None, factors=[],
            ),
        },
        {
            "source_text": "中兴通讯云计算软件开发",
            "extraction": golden(
                company="中兴通讯", position="云计算软件开发", history_step="投递",
                history_date="2026-08-03", history_summary="投递中兴通讯云计算软件开发",
                projected_stage="applied", projected_step="完成投递", next_action=None,
                questions=[], mood=None, time_of_day=None, factors=[],
            ),
        },
        {
            "source_text": "金山办公协同后端",
            "extraction": golden(
                company="金山办公", position="协同后端", history=None,
                projected_state={"stage": "written_test", "current_step": None},
                next_action={
                    "stage": "written_test", "step": "在线测评", "date": "2026-08-04",
                    "time": "19:00", "note": None,
                },
                questions=[], mood=None, time_of_day=None, factors=[],
            ),
        },
        {
            "source_text": "顺丰科技物流平台后端",
            "extraction": golden(
                company="顺丰科技", position="物流平台后端", history_step="HR 初筛",
                history_date="2026-08-03", history_outcome="passed",
                history_summary="顺丰科技物流平台后端通过 HR 初筛",
                projected_stage="interviewing", projected_step="HR 初筛",
                next_action={
                    "stage": "interviewing", "step": "等待一面安排", "date": None,
                    "time": None, "note": None,
                },
                questions=[], mood=None, time_of_day=None, factors=[],
            ),
        },
    ]}
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 29, allow_batch=True).arun({"text": message}))

    assert response.status == "partial"
    assert "拆成 5 条独立复盘方案" in response.text
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert len(pending) == 5
    assert {
        item["preview"]["extraction"]["company"] for item in pending
    } == {"荣耀", "爱奇艺", "中兴通讯", "金山办公", "顺丰科技"}


def _unavailable_role_batch(batch: dict, unavailable_company: str):
    """Script a batch whose targeted call for one role never reaches the provider."""
    script = batch_script(batch)
    surviving = [
        line for index, line in enumerate(script[1:])
        if batch["items"][index]["extraction"]["company"] != unavailable_company
    ]

    class OneRoleUnavailableLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            payload = str(messages[-1].get("content") or "")
            if f'"company":"{unavailable_company}"' in payload:
                raise LLMRequestError("provider outage")
            return self._next()

    return OneRoleUnavailableLLM([script[0], *surviving])


def test_an_unavailable_role_is_reported_while_the_others_are_still_staged(db_path):
    message = "投递甲公司的前端岗位；投递乙公司的后端岗位；投递丙公司的算法岗位"
    batch = {"items": [
        {
            "source_text": f"{company}的{position}",
            "extraction": golden(
                company=company, position=position, history_step="投递",
                history_summary=f"投递{company}{position}", projected_stage="applied",
                projected_step="完成投递", next_action=None, questions=[], mood=None,
                time_of_day=None, factors=[],
            ),
        }
        for company, position in (("甲公司", "前端"), ("乙公司", "后端"), ("丙公司", "算法"))
    ]}
    service = ReviewService(db_path, _unavailable_role_batch(batch, "乙公司"))

    response = run(record_tool(service, 32, allow_batch=True).arun({"text": message}))

    assert response.status == "partial"
    assert response.data["skipped_count"] == 1
    assert "乙公司 · 后端" in response.text
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert {item["preview"]["extraction"]["company"] for item in pending} == {
        "甲公司", "丙公司",
    }


def test_a_batch_whose_roles_are_all_unavailable_writes_nothing(db_path):
    message = "投递甲公司的前端岗位；投递乙公司的后端岗位"
    batch = {"items": [
        {
            "source_text": f"{company}的{position}",
            "extraction": golden(
                company=company, position=position, history_step="投递",
                history_summary=f"投递{company}{position}", projected_stage="applied",
                projected_step="完成投递", next_action=None, questions=[], mood=None,
                time_of_day=None, factors=[],
            ),
        }
        for company, position in (("甲公司", "前端"), ("乙公司", "后端"))
    ]}

    class EveryRoleUnavailableLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            payload = str(messages[-1].get("content") or "")
            if '"target"' in payload:
                raise LLMRequestError("provider outage")
            return self._next()

    service = ReviewService(db_path, EveryRoleUnavailableLLM(batch_script(batch)[:1]))

    response = run(record_tool(service, 33, allow_batch=True).arun({"text": message}))

    assert response.status == "error"
    assert response.data["reason"] == "batch_review_extraction_unavailable"
    assert response.data["failure_kind"] == "provider_request"
    assert count(db_path, "SELECT COUNT(*) FROM journal") == 0


def test_incomplete_identity_manifest_is_rejected_before_any_write(db_path):
    message = "今天投了甲公司的前端岗位和乙公司的后端岗位"
    incomplete_manifest = {"items": [{
        "source_text": "甲公司的前端",
        "company": "甲公司",
        "position": "前端",
    }]}
    service = ReviewService(db_path, ScriptedLLM([
        json.dumps(incomplete_manifest, ensure_ascii=False),
    ]))

    response = run(record_tool(service, 30, allow_batch=True).arun({"text": message}))

    assert response.status == "error"
    assert response.data == {"reason": "batch_review_record_validation_failed"}
    assert count(db_path, "SELECT COUNT(*) FROM journal") == 0


def test_target_identity_mismatch_rejects_the_whole_batch_before_any_write(db_path):
    message = "投递甲公司的前端岗位；投递乙公司的后端岗位"
    batch = {"items": [
        {
            "source_text": "甲公司的前端",
            "extraction": golden(
                company="甲公司", position="前端", history_step="投递",
                history_summary="投递甲公司前端", projected_stage="applied",
                projected_step="完成投递", next_action=None, questions=[], mood=None,
                time_of_day=None, factors=[],
            ),
        },
        {
            "source_text": "乙公司的后端",
            "extraction": golden(
                company="错误公司", position="后端", history_step="投递",
                history_summary="投递乙公司后端", projected_stage="applied",
                projected_step="完成投递", next_action=None, questions=[], mood=None,
                time_of_day=None, factors=[],
            ),
        },
    ]}
    manifest = {"items": [
        {"source_text": "甲公司的前端", "company": "甲公司", "position": "前端"},
        {"source_text": "乙公司的后端", "company": "乙公司", "position": "后端"},
    ]}
    service = ReviewService(db_path, ScriptedLLM([
        json.dumps(manifest, ensure_ascii=False),
        *(json.dumps(item["extraction"], ensure_ascii=False) for item in batch["items"]),
    ]))

    response = run(record_tool(service, 31, allow_batch=True).arun({"text": message}))

    assert response.status == "error"
    assert response.data == {"reason": "batch_review_record_validation_failed"}
    assert count(db_path, "SELECT COUNT(*) FROM journal") == 0


def test_batch_preview_publication_rolls_back_every_item_on_late_failure(db_path):
    message = "投递甲公司的前端岗位；投递乙公司的后端岗位"
    batch = {"items": [
        {
            "source_text": "甲公司的前端",
            "extraction": golden(
                company="甲公司", position="前端", history_step="投递",
                history_summary="投递甲公司前端", projected_stage="applied",
                projected_step="完成投递", next_action=None, questions=[], mood=None,
                time_of_day=None, factors=[],
            ),
        },
        {
            "source_text": "乙公司的后端",
            "extraction": golden(
                company="乙公司", position="后端", history_step="投递",
                history_summary="投递乙公司后端", projected_stage="applied",
                projected_step="完成投递", next_action=None, questions=[], mood=None,
                time_of_day=None, factors=[],
            ),
        },
    ]}
    with transaction(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_second_review_preview "
            "BEFORE UPDATE OF state ON journal "
            "WHEN NEW.kind = 'correction' AND NEW.state = 'awaiting_user' "
            "AND (SELECT content FROM journal WHERE id = NEW.parent_journal_id) "
            "LIKE '%乙公司%' "
            "BEGIN SELECT RAISE(ABORT, 'second preview unavailable'); END",
        )
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    with pytest.raises(sqlite3.IntegrityError, match="second preview unavailable"):
        run(service.execute_batch_record_operations(
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000032",
            text=message,
            today="2026-08-03",
        ))

    assert count(db_path, "SELECT COUNT(*) FROM journal") == 0


def test_record_review_tool_accepts_fifty_model_returned_job_parameters(db_path):
    source_items = [f"投递公司{index:02d}的岗位{index:02d}" for index in range(50)]
    message = "；".join(source_items)
    batch = {
        "items": [
            {
                "source_text": source_text,
                "extraction": golden(
                    company=f"公司{index:02d}", position=f"岗位{index:02d}",
                    history_step="投递", history_summary=source_text,
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            }
            for index, source_text in enumerate(source_items)
        ],
    }
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 23, allow_batch=True).arun({"text": message}))

    assert response.status == "partial"
    assert "拆成 50 条独立复盘方案" in response.text
    assert len(response.data["operation_ids"]) == 50
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert len(pending) == 50
    assert {
        item["preview"]["extraction"]["company"]
        for item in pending
    } == {f"公司{index:02d}" for index in range(50)}


def test_shared_date_and_channel_are_retained_for_each_parallel_application(db_path):
    message = "我昨天在Boss直聘投递了甲公司的前端岗位，以及乙公司的后端岗位"
    batch = {
        "items": [
            {
                "source_text": "我昨天在Boss直聘投递了甲公司的前端岗位",
                "extraction": golden(
                    company="甲公司", position="前端", channel="Boss直聘",
                    history_step="投递", history_date="2026-07-17",
                    history_summary="投递甲公司前端",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
            {
                "source_text": "以及乙公司的后端岗位",
                "extraction": golden(
                    company="乙公司", position="后端", channel="Boss直聘",
                    history_step="投递", history_date="2026-07-17",
                    history_summary="投递乙公司后端",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
        ],
    }
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 24, allow_batch=True).arun({"text": message}))

    assert response.status == "partial"
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert {
        (
            item["preview"]["extraction"]["company"],
            item["preview"]["extraction"]["channel"],
            item["preview"]["extraction"]["history"]["date"],
        )
        for item in pending
    } == {
        ("甲公司", "Boss直聘", "2026-07-17"),
        ("乙公司", "Boss直聘", "2026-07-17"),
    }


def test_batch_rejects_a_summary_that_contains_another_item_company(db_path):
    message = "投递甲公司的前端岗位；投递乙公司的后端岗位"
    batch = {
        "items": [
            {
                "source_text": "投递甲公司的前端岗位",
                "extraction": golden(
                    company="甲公司", position="前端",
                    history_step="投递",
                    history_summary="投递甲公司前端，同时投递乙公司后端",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[],
                    next_action=None,
                ),
            },
            {
                "source_text": "投递乙公司的后端岗位",
                "extraction": golden(
                    company="乙公司", position="后端", history_step="投递",
                    history_summary="投递乙公司后端",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[], next_action=None,
                ),
            },
        ],
    }
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 25, allow_batch=True).arun({"text": message}))

    assert response.status == "error"
    assert response.data == {"reason": "batch_review_record_validation_failed"}
    assert count(db_path, "SELECT COUNT(*) FROM journal") == 0


def test_batch_rejects_another_item_company_hidden_in_question_text(db_path):
    message = "面试甲公司的前端岗位；面试乙公司的后端岗位"
    first = golden(
        company="甲公司", position="前端", history_summary="甲公司前端面试完成",
        questions=[{
            "text": "乙公司的后端岗位使用了什么架构？",
            "stuck": False,
            "knowledge_points": ["乙公司架构"],
        }],
        mood=None, time_of_day=None, factors=[], next_action=None,
    )
    second = golden(
        company="乙公司", position="后端", history_summary="乙公司后端面试完成",
        questions=[], mood=None, time_of_day=None, factors=[],
        next_action=None,
    )
    batch = {"items": [
        {"source_text": "面试甲公司的前端岗位", "extraction": first},
        {"source_text": "面试乙公司的后端岗位", "extraction": second},
    ]}
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 26, allow_batch=True).arun({"text": message}))

    assert response.status == "error"
    assert response.data == {"reason": "batch_review_record_validation_failed"}
    assert count(db_path, "SELECT COUNT(*) FROM journal") == 0


@pytest.mark.parametrize(
    ("message", "items"),
    [
        (
            "投递清华的产品岗位；投递清华大学的教授岗位",
            [
                ("我投递了清华的产品岗位", "清华", "产品"),
                ("投递清华大学的教授岗位", "清华大学", "教授"),
            ],
        ),
        (
            "投递清华大学的教授岗位；投递清华的产品岗位",
            [
                ("我投递了清华大学的教授岗位", "清华大学", "教授"),
                ("我投递了清华的产品岗位", "清华", "产品"),
            ],
        ),
    ],
)
def test_prefix_company_names_reanchor_to_their_full_original_occurrences(
    db_path,
    message,
    items,
):
    batch = {
        "items": [
            {
                "source_text": source_text,
                "extraction": golden(
                    company=company, position=position, history_step="投递",
                    history_summary=f"投递{company}{position}",
                    projected_stage="applied", projected_step="完成投递",
                    questions=[], mood=None, time_of_day=None, factors=[], next_action=None,
                ),
            }
            for source_text, company, position in items
        ],
    }
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 27, allow_batch=True).arun({"text": message}))

    assert response.status == "partial"
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert {
        (
            item["preview"]["extraction"]["company"],
            item["preview"]["extraction"]["position"],
        )
        for item in pending
    } == {("清华", "产品"), ("清华大学", "教授")}


def test_same_company_parallel_positions_use_their_own_position_evidence(db_path):
    message = "我投了腾讯的前端岗位，以及后端岗位"
    batch = {"items": [
        {
            "source_text": "我投了腾讯的前端岗位",
            "extraction": golden(
                company="腾讯", position="前端", history_step="投递",
                history_summary="投递腾讯前端",
                projected_stage="applied", projected_step="完成投递",
                questions=[], mood=None, time_of_day=None, factors=[], next_action=None,
            ),
        },
        {
            "source_text": "我投了腾讯的后端岗位",
            "extraction": golden(
                company="腾讯", position="后端", history_step="投递",
                history_summary="投递腾讯后端",
                projected_stage="applied", projected_step="完成投递",
                questions=[], mood=None, time_of_day=None, factors=[], next_action=None,
            ),
        },
    ]}
    service = ReviewService(db_path, ScriptedLLM(batch_script(batch)))

    response = run(record_tool(service, 28, allow_batch=True).arun({"text": message}))

    assert response.status == "partial"
    pending = list_pending_review_record_confirmations(db_path, "u1")
    assert {
        (item["preview"]["extraction"]["company"], item["preview"]["extraction"]["position"])
        for item in pending
    } == {("腾讯", "前端"), ("腾讯", "后端")}
    with read_connection(db_path) as conn:
        sources = {
            row[0]
            for row in conn.execute("SELECT content FROM journal WHERE kind='review'").fetchall()
        }
    assert "后端" in sources
    assert "我投了腾讯的后端岗位" not in sources


def test_record_review_tool_clarify_then_supplement(db_path):
    script = [json.dumps(golden(company=None, position=None), ensure_ascii=False),
              json.dumps(GOLDEN_EXTRACTION, ensure_ascii=False)]
    service = ReviewService(db_path, ScriptedLLM(script))
    tool = record_tool(service, 2)

    asked = run(tool.arun({"text": GOLDEN_TEXT}))
    assert asked.status == "partial"
    review_reference = asked.data["operation_id"]
    assert "尚不能安全定位岗位" in asked.text and "编辑或排除" in asked.text
    assert review_reference not in asked.text

    next_turn_tool = record_tool(
        service,
        3,
        review_supplement_reference=review_reference,
    )
    done = run(next_turn_tool.arun({"text": "是字节的 LLM 应用岗"}))
    assert done.status == "partial"
    assert done.data["state"] == "pending_confirmation"
    approved = approve_review_record_operation(db_path, "u1", done.data["operation_id"])
    assert approved["outcome"] == "applied"
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries WHERE step='二面'") == 1


def test_pending_review_does_not_block_an_unrelated_independent_progress(
    db_path,
):
    service = ReviewService(
        db_path,
        ScriptedLLM([
            json.dumps(golden(company=None), ensure_ascii=False),
            json.dumps(golden(company="终验科技", position="Agent 工程师"), ensure_ascii=False),
        ]),
    )
    asked = run(record_tool(service, 15).arun({"text": GOLDEN_TEXT}))

    independent = run(record_tool(service, 16).arun({
        "text": "另外今天投了终验科技的 Agent 工程师",
    }))

    assert asked.status == "partial"
    assert independent.status == "partial"
    assert independent.data["state"] == "pending_confirmation"
    assert count(db_path, "SELECT COUNT(*) FROM journal WHERE kind='review'") == 2


def test_page_bound_supplement_executes_deterministically_and_commits_chat_history(
    db_path,
    monkeypatch,
):
    from careerdesk.orchestration.assistant import service as assistant_module

    initial = ReviewService(
        db_path,
        ScriptedLLM([json.dumps(golden(company=None), ensure_ascii=False)]),
    )
    asked = run(record_tool(initial, 17).arun({"text": GOLDEN_TEXT}))
    reference = asked.data["review_reference"]
    supplement_llm = ScriptedLLM([
        json.dumps(golden(company="终验科技", position="Agent 工程师"), ensure_ascii=False),
    ])
    monkeypatch.setattr(
        assistant_module,
        "get_settings",
        lambda: SimpleNamespace(db_path=db_path, llm_model="test:model", strict_offline=False),
    )
    monkeypatch.setattr(
        "careerdesk.platform.ai.client.build_llm",
        lambda _model, **_kwargs: supplement_llm,
    )
    monkeypatch.setattr(
        "careerdesk.agentic.agents.build_career_assistant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("page-bound supplement must not depend on agent routing"),
        ),
    )

    async def collect():
        return [event async for event in run_chat(
            "终验科技，Agent 工程师。",
            "bound-review-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000018",
            review_supplement_reference=reference,
        )]

    events = run(collect())

    status = next(event for event in events if event.event == "tool_status")
    assert status.data["trusted_operation_type"] == "review_record"
    assert status.data["label"] == "整理进展…"
    direct_reply = "".join(
        event.data["text"]
        for event in events
        if event.event == "message_delta"
    )
    assert "请让用户" not in direct_reply
    assert "统一确认" in direct_reply
    assert any(
        event.event == "message_delta" and "复盘方案" in event.data["text"]
        for event in events
    )
    assert events[-1].event == "done"
    assert count(db_path, "SELECT COUNT(*) FROM journal WHERE kind='review'") == 1
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 0
    [pending] = list_pending_review_record_confirmations(db_path, "u1")
    approved = approve_review_record_operation(db_path, "u1", pending["operation_id"])
    assert approved["outcome"] == "applied"
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1
    import sqlite3 as _sqlite3
    from contextlib import closing

    with closing(_sqlite3.connect(db_path)) as conn:
        history = conn.execute(
            "SELECT role, content FROM session_messages "
            "WHERE sc_user='u1' AND sc_session='bound-review-session' ORDER BY rowid",
        ).fetchall()
    assert [row[0] for row in history] == ["user", "assistant"]
    assert history[0][1] == "终验科技，Agent 工程师。"


class _FakeReviewService:

    def __init__(self, *, process_error: BaseException | None = None, pending: bool = False):
        self.process_error = process_error
        self.pending = pending
        self.record_calls: list[tuple[str, str, str | None]] = []

    def has_pending_record_clarifications(self, _user_id: str) -> bool:
        return self.pending

    async def execute_record_operation(
        self,
        user_id: str,
        *,
        operation_id: str,
        client_turn_id: str,
        text: str,
        review_reference: str | None,
    ) -> dict:
        del operation_id, client_turn_id
        self.record_calls.append((user_id, text, review_reference))
        if self.process_error is not None:
            raise self.process_error
        return {
            "state": "failed",
            "error": {"message": "fake process result"},
        }


def test_record_review_single_write_budget_blocks_second_process_before_service():
    service = _FakeReviewService()
    tool = record_tool(service, 4)

    first = run(tool.arun({"text": "投了甲公司后端"}))
    second = run(tool.arun({"text": "又投了乙公司前端"}))

    assert first.text == "复盘原话已保存，但这次整理没有完成，也没有写入岗位、时间线或题库。"
    assert second.status == "error"
    assert second.data == {"reason": "single_write_budget_exhausted"}
    assert "parse_jobs" in second.text and "下一轮" in second.text
    assert service.record_calls == [("u1", "投了甲公司后端", None)]


def test_record_review_failed_first_attempt_still_consumes_budget():
    service = _FakeReviewService(process_error=RuntimeError("extractor unavailable"))
    tool = record_tool(service, 5)

    with pytest.raises(RuntimeError, match="extractor unavailable"):
        run(tool.arun({"text": "投了甲公司后端"}))
    second = run(tool.arun({"text": "重试甲公司后端"}))

    assert second.data == {"reason": "single_write_budget_exhausted"}
    assert service.record_calls == [("u1", "投了甲公司后端", None)]


def test_unbound_record_review_ignores_unrelated_pending_clarification():
    service = _FakeReviewService(pending=True)
    tool = record_tool(service, 13)

    response = run(tool.arun({"text": "是终验科技的 Agent 工程师"}))

    assert response.status == "error"
    assert response.data != {"reason": "pending_review_requires_binding"}
    assert service.record_calls == [("u1", "是终验科技的 Agent 工程师", None)]


def test_page_bound_supplement_bypasses_pending_creation_guard():
    service = _FakeReviewService(pending=True)
    bound = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    tool = record_tool(service, 14, review_supplement_reference=bound)

    response = run(tool.arun({"text": "是终验科技的 Agent 工程师"}))

    assert response.status == "error"
    assert response.data != {"reason": "pending_review_requires_binding"}
    assert service.record_calls == [("u1", "是终验科技的 Agent 工程师", bound)]


@pytest.mark.parametrize(
    "text",
    [
        "说错了，是二面",
        "刚才记错了，不是一面，是二面",
        "昨天说错了，腾讯那场其实是第二轮",
    ],
)
def test_unbound_record_review_rejects_existing_review_correction(text):
    service = _FakeReviewService()
    tool = record_tool(service, 19)

    response = run(tool.arun({"text": text}))

    assert response.status == "error"
    assert response.data == {"reason": "review_correction_requires_edit"}
    assert "manage_review" in response.text
    assert service.record_calls == []


def test_record_review_never_echoes_validation_error_input_fragments():
    secret = "SENSITIVE_TOKEN_SHOULD_NOT_RETURN"
    service = _FakeReviewService(process_error=ValueError(
        f"validation failed input_value='{secret}' https://errors.pydantic.dev/internal",
    ))
    tool = record_tool(service, 11)

    response = run(tool.arun({"text": "投了甲公司后端"}))

    assert response.status == "error"
    assert response.data == {"reason": "review_record_validation_failed"}
    assert secret not in response.text
    assert "pydantic" not in response.text


def test_record_review_budget_cannot_be_bypassed_with_supplement_id():
    service = _FakeReviewService()
    tool = record_tool(service, 6)

    run(tool.arun({"text": "投了甲公司后端"}))
    blocked = run(tool.arun({"text": "伪造补充", "supplement_to": 999_999}))

    assert blocked.data == {"reason": "single_write_budget_exhausted"}
    assert service.record_calls == [("u1", "投了甲公司后端", None)]


def test_record_review_new_request_instance_restores_one_attempt():
    service = _FakeReviewService()

    run(record_tool(service, 7).arun({"text": "第一轮单条进展"}))
    run(record_tool(service, 8).arun({"text": "下一轮单条进展"}))

    assert service.record_calls == [
        ("u1", "第一轮单条进展", None),
        ("u1", "下一轮单条进展", None),
    ]


@pytest.mark.parametrize("text", [None, True, "", "   "])
def test_record_review_invalid_text_consumes_attempt_before_service(text):
    service = _FakeReviewService()
    tool = record_tool(service, 9)

    invalid = run(tool.arun({"text": text}))
    blocked = run(tool.arun({"text": "不能借校验失败在同轮重试"}))

    assert invalid.status == "error"
    assert invalid.data == {"reason": "invalid_review_text"}
    assert blocked.data == {"reason": "single_write_budget_exhausted"}
    assert service.record_calls == []


@pytest.mark.parametrize("reference", [True, 1, "不是 UUID", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"])
def test_record_review_rejects_any_model_supplied_supplement_reference(reference):
    service = _FakeReviewService()
    tool = record_tool(service, 10)

    invalid = run(tool.arun({"text": "补充回答", "supplement_to": reference}))

    assert invalid.status == "error"
    assert invalid.data == {"reason": "untrusted_review_parameters"}
    assert service.record_calls == []


def test_record_review_bound_supplement_is_hidden_and_cannot_be_overwritten():
    service = _FakeReviewService()
    bound = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    model_supplied = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
    tool = record_tool(service, 12, review_supplement_reference=bound)

    response = run(tool.arun({
        "text": "确认是二面",
        "supplement_to": model_supplied,
    }))

    assert {parameter.name for parameter in tool.get_parameters()} == {"text"}
    assert response.status == "error"
    assert response.data == {"reason": "untrusted_review_parameters"}
    assert service.record_calls == []


def test_main_agent_end_to_end_records_review(db_path):
    llm = ScriptedLLM([
        ScriptedLLM.tool_call("record_review", {}),
        *batch_script(single_item_batch(GOLDEN_TEXT, GOLDEN_EXTRACTION)),
        FINAL_REPLY,
    ])
    agent = build_career_assistant(
        db_path,
        llm,
        "u1",
        client_turn_id=AGENT_TURN_ID,
        trusted_review_source=GOLDEN_TEXT,
    )
    result = run(agent.arun(GOLDEN_TEXT, scope=Scope(user="u1", app="careerdesk", session="s1")))
    assert result.final_output == FINAL_REPLY
    assert llm.calls == 4
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    [pending] = list_pending_review_record_confirmations(db_path, "u1")
    approve_review_record_operation(db_path, "u1", pending["operation_id"])
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1
    assert count(db_path, "SELECT COUNT(*) FROM questions WHERE source='real'") == 1


def test_run_chat_streams_tool_status_and_deltas(db_path):
    def factory(hooks):
        llm = ScriptedLLM([
            ScriptedLLM.tool_call("record_review", {}),
            *batch_script(single_item_batch(GOLDEN_TEXT, GOLDEN_EXTRACTION)),
            FINAL_REPLY,
        ])
        return build_career_assistant(
            db_path,
            llm,
            "u1",
            client_turn_id=AGENT_TURN_ID,
            trusted_review_source=GOLDEN_TEXT,
            hooks=hooks,
        )

    async def collect():
        return [event async for event in run_chat(
            GOLDEN_TEXT, "review-session", "u1", client_turn_id="review-turn",
            agent_factory=factory,
        )]

    events = run(collect())
    kinds = [event.event for event in events]
    assert "tool_status" in kinds
    tool_event = next(event for event in events if event.event == "tool_status")
    assert tool_event.data == {
        "tool": "record_review",
        "label": "整理进展…",
        "trusted_operation_type": "review_record",
    }
    assert tool_event.data["trusted_operation_type"] == "review_record"
    final_text = "".join(event.data["text"] for event in events if event.event == "message_delta")
    assert final_text == FINAL_REPLY
    assert kinds[-1] == "done" and events[-1].data["session"]
    assert count(
        db_path,
        "SELECT COUNT(*) FROM session_messages WHERE sc_user=? AND sc_session=?",
        "u1",
        "review-session",
    ) == 2


def test_chat_replaces_an_unverified_stage_update_claim_with_server_truth(db_path):
    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "已更新，香港浸会大学已回到笔试阶段。"

    async def collect():
        return [event async for event in run_chat(
            "把香港浸会大学的申请改回笔试阶段",
            "unverified-write-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000114",
            agent_factory=lambda _hooks: Agent(),
        )]

    events = run(collect())
    final_text = rendered_chat_text(events)
    assert "没有取得支持该完成声明的可信回执" in final_text
    assert "已回到笔试阶段" not in final_text
    assert not any(
        event.event == "message_delta" and "已更新" in event.data["text"]
        for event in events
    )
    assert events[-1].event == "done"


def test_chat_releases_stage_update_claim_after_matching_trusted_receipt(db_path):
    def factory(hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope):
                hooks[0].after_tool(
                    "update_application",
                    {"updates": [{
                        "company": "香港浸会大学",
                        "position": "副教授",
                        "new_stage": "written_test",
                    }]},
                    ToolResponse.ok(
                        "ok",
                        data=completed_application_update_batch(
                            "香港浸会大学", "副教授", "written_test",
                            client_turn_id="00000000-0000-4000-8000-000000000115",
                        ),
                    ),
                )
                yield "已更新，香港浸会大学已回到笔试阶段。"

        return Agent()

    async def collect():
        return [event async for event in run_chat(
            "把香港浸会大学的申请改回笔试阶段",
            "verified-write-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000115",
            agent_factory=factory,
        )]

    events = run(collect())
    final_text = "".join(
        event.data["text"] for event in events if event.event == "message_delta"
    )
    assert final_text == "已更新，香港浸会大学已回到笔试阶段。"
    assert events[-1].event == "done"


def test_verified_write_streams_before_model_generation_finishes(db_path):
    reply = (
        "已更新，测试公司的申请已回到笔试阶段。"
        "你可以继续准备笔试，并在收到新的安排后补充下一步日期和说明。"
    )
    first_piece = "已更新，测试公司的申请已回到笔试阶段。"
    allow_tool_finish = asyncio.Event()
    allow_generation_finish = asyncio.Event()
    state = {
        "tool_finished": False,
        "generation_finished": False,
        "buffer_output": None,
    }

    def factory(hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope, buffer_output=False):
                state["buffer_output"] = buffer_output
                parameters = {"updates": [{
                    "company": "测试公司",
                    "position": "测试工程师",
                    "new_stage": "written_test",
                }]}
                hooks[0].before_tool("update_application", parameters)
                await allow_tool_finish.wait()
                hooks[0].after_tool(
                    "update_application",
                    parameters,
                    ToolResponse.ok(
                        "ok",
                        data=completed_application_update_batch(
                            "测试公司", "测试工程师", "written_test",
                            client_turn_id="00000000-0000-4000-8000-000000000405",
                        ),
                    ),
                )
                state["tool_finished"] = True
                yield first_piece
                await allow_generation_finish.wait()
                yield reply[len(first_piece):]
                state["generation_finished"] = True

        return Agent()

    async def collect():
        observed = []
        async for event in run_chat(
            "把测试公司的申请改回笔试阶段",
            "paced-write-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000405",
            agent_factory=factory,
        ):
            observed.append((event, dict(state)))
            if event.event == "tool_status":
                allow_tool_finish.set()
            elif event.event == "message_delta" and not allow_generation_finish.is_set():
                allow_generation_finish.set()
        return observed

    observed = run(collect())
    status = next(item for item in observed if item[0].event == "tool_status")
    deltas = [item for item in observed if item[0].event == "message_delta"]

    assert status[0].data["tool"] == "update_application"
    assert status[1]["tool_finished"] is False
    assert state["buffer_output"] is False
    assert len(deltas) >= 2
    assert deltas[0][1]["tool_finished"] is True
    assert deltas[0][1]["generation_finished"] is False
    assert "".join(item[0].data["text"] for item in deltas) == reply
    assert state["generation_finished"] is True
    assert observed[-1][0].event == "done"


def test_read_only_reply_streams_before_model_generation_finishes(db_path):
    first_piece = "这是模型正在生成的第一段回答，后面还有内容。"
    second_piece = "这是生成完成前的第二段回答。"
    allow_generation_finish = asyncio.Event()
    state = {"generation_finished": False}

    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield first_piece
            await allow_generation_finish.wait()
            yield second_piece
            state["generation_finished"] = True

    async def collect():
        observed = []
        async for event in run_chat(
            "给我一些面试建议",
            "live-read-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000407",
            agent_factory=lambda _hooks: Agent(),
        ):
            observed.append((event, state["generation_finished"]))
            if event.event == "message_delta" and not allow_generation_finish.is_set():
                allow_generation_finish.set()
        return observed

    observed = run(collect())
    deltas = [item for item in observed if item[0].event == "message_delta"]

    assert deltas[0][0].data["text"] == first_piece
    assert deltas[0][1] is False
    assert "".join(item[0].data["text"] for item in deltas) == first_piece + second_piece
    assert state["generation_finished"] is True
    assert observed[-1][0].event == "done"


def test_code_heavy_reply_streams_before_model_generation_finishes(db_path):
    first_piece = "Result: " + "1234567890" * 20
    second_piece = " Done."
    allow_generation_finish = asyncio.Event()
    state = {"generation_finished": False}

    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield first_piece
            await allow_generation_finish.wait()
            yield second_piece
            state["generation_finished"] = True

    async def collect():
        observed = []
        async for event in run_chat(
            "Show the result",
            "code-heavy-live-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000408",
            output_locale="en",
            agent_factory=lambda _hooks: Agent(),
        ):
            observed.append((event, state["generation_finished"]))
            if event.event == "message_delta" and not allow_generation_finish.is_set():
                allow_generation_finish.set()
        return observed

    observed = run(collect())
    deltas = [item for item in observed if item[0].event == "message_delta"]

    assert deltas[0][1] is False
    assert deltas[0][0].data["text"] == first_piece
    assert "".join(item[0].data["text"] for item in deltas) == first_piece + second_piece
    assert state["generation_finished"] is True


def test_streaming_gate_replaces_an_unverified_claim_without_publishing_it(db_path):
    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "这是可以立即展示的安全状态说明，"
            yield "已"
            yield "更新，但其实没有任何可信写入回执。"

    async def collect():
        return [event async for event in run_chat(
            "只告诉我当前状态",
            "stream-claim-guard-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000406",
            agent_factory=lambda _hooks: Agent(),
        )]

    events = run(collect())
    deltas = [event.data["text"] for event in events if event.event == "message_delta"]
    snapshots = [event.data["text"] for event in events if event.event == "message_snapshot"]

    assert deltas
    assert "已更新" not in "".join(deltas)
    assert snapshots[-1].startswith("这次没有取得")
    assert rendered_chat_text(events) == snapshots[-1]
    assert events[-1].event == "done"


def test_streaming_gate_never_releases_a_subject_prefixed_unverified_claim(db_path):
    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "微众银行以外的其他岗位状态都"
            yield "已更新"

    async def collect():
        return [event async for event in run_chat(
            "帮我更新五个岗位的最新进展",
            "subject-prefixed-claim-guard-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000412",
            agent_factory=lambda _hooks: Agent(),
        )]

    events = run(collect())
    deltas = [event.data["text"] for event in events if event.event == "message_delta"]

    assert "已更新" not in "".join(deltas)
    assert rendered_chat_text(events).startswith("这次没有取得支持该完成声明的可信回执")
    assert events[-1].event == "done"


def test_streaming_gate_releases_a_late_verified_subject_prefixed_claim(db_path):
    turn_id = "00000000-0000-4000-8000-000000000413"

    def factory(hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope):
                yield "测试公司的岗位状态都已更新"
                hooks[0].after_tool(
                    "update_application",
                    {"updates": [{
                        "company": "测试公司",
                        "position": "测试工程师",
                        "new_stage": "offer",
                    }]},
                    ToolResponse.ok(
                        "ok",
                        data=completed_application_update_batch(
                            "测试公司",
                            "测试工程师",
                            "offer",
                            client_turn_id=turn_id,
                        ),
                    ),
                )

        return Agent()

    async def collect():
        return [event async for event in run_chat(
            "把测试公司岗位改成 Offer 阶段",
            "late-verified-subject-claim-session",
            "u1",
            client_turn_id=turn_id,
            agent_factory=factory,
        )]

    events = run(collect())

    assert rendered_chat_text(events) == "测试公司的岗位状态都已更新"
    assert events[-1].event == "done"


def test_streaming_gate_blocks_curly_apostrophe_completion_claim(db_path):
    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "This safe preamble has enough ordinary English words to begin streaming now. "
            yield "I’"
            yield "ve updated the application without a trusted receipt."

    async def collect():
        return [event async for event in run_chat(
            "Only report the current status",
            "stream-english-claim-guard-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000409",
            output_locale="en",
            agent_factory=lambda _hooks: Agent(),
        )]

    events = run(collect())
    deltas = [event.data["text"] for event in events if event.event == "message_delta"]

    assert deltas
    assert "I’ve updated" not in "".join(deltas)
    assert rendered_chat_text(events).startswith("I did not receive a trusted receipt")
    assert events[-1].event == "done"


def test_streaming_gate_rejects_unsupported_claim_after_verified_write(db_path):
    def factory(hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope):
                hooks[0].after_tool(
                    "update_application",
                    {"updates": [{
                        "company": "测试公司",
                        "position": "测试工程师",
                        "new_stage": "offer",
                    }]},
                    ToolResponse.ok(
                        "ok",
                        data=completed_application_update_batch(
                            "测试公司", "测试工程师", "offer",
                            client_turn_id="00000000-0000-4000-8000-000000000411",
                        ),
                    ),
                )
                yield "岗位字段已正常更新。"
                yield "我已经删除了这个岗位。"

        return Agent()

    async def collect():
        return [event async for event in run_chat(
            "更新测试公司的岗位字段",
            "stream-operation-claim-guard-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000411",
            agent_factory=factory,
        )]

    events = run(collect())
    deltas = [event.data["text"] for event in events if event.event == "message_delta"]

    assert "已经删除" not in "".join(deltas)
    assert rendered_chat_text(events).startswith("这次没有取得")
    assert events[-1].event == "done"


def test_exact_proposal_identity_is_durable_in_live_done_and_replay(db_path):
    operation_id = "00000000-0000-4000-8000-000000000110"
    turn_id = "00000000-0000-4000-8000-000000000111"
    calls = 0

    def factory(hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope):
                nonlocal calls
                calls += 1
                hooks[0].after_tool(
                    "delete_application",
                    {},
                    ToolResponse.ok("ok", data={
                        "operation_type": "application_delete",
                        "operation_id": operation_id,
                        "state": "pending",
                    }),
                )
                yield "请核对删除方案"

        return Agent()

    async def collect():
        return [event async for event in run_chat(
            "删除目标岗位",
            "durable-proposal-session",
            "u1",
            client_turn_id=turn_id,
            agent_factory=factory,
        )]

    first = run(collect())
    proposal_status = next(
        event for event in first
        if event.event == "tool_status" and event.data["tool"] == "proposal_ready"
    )
    assert proposal_status.data == {
        "tool": "proposal_ready",
        "label": "方案已准备，等待你确认…",
        "proposal_surface": "application_delete",
        "proposal_operation_id": operation_id,
    }
    expected = [{
        "surface": "application_delete",
        "operation_id": operation_id,
    }]
    assert first[-1].event == "done"
    assert first[-1].data["proposal_operations"] == expected
    snapshot = assistant_ledger.read_turn_status(db_path, "u1", turn_id)
    assert list(snapshot.proposal_operations) == expected

    replayed = run(collect())
    assert calls == 1
    assert [event.event for event in replayed] == ["message_snapshot", "done"]
    assert replayed[-1].data["proposal_operations"] == expected


def test_unknown_turn_status_recovers_proposal_created_before_sse_disconnect(db_path):
    operation_id = "00000000-0000-4000-8000-000000000112"
    turn_id = "00000000-0000-4000-8000-000000000113"

    def factory(hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope):
                hooks[0].after_tool(
                    "manage_review",
                    {},
                    ToolResponse.ok("ok", data={
                        "operation_type": "review_undo",
                        "operation_id": operation_id,
                        "state": "pending",
                    }),
                )
                raise RuntimeError("connection disappeared after tool commit")
                yield  # pragma: no cover

        return Agent()

    async def collect():
        return [event async for event in run_chat(
            "撤销复盘",
            "unknown-proposal-session",
            "u1",
            client_turn_id=turn_id,
            agent_factory=factory,
        )]

    events = run(collect())
    assert events[-1].event == "error"
    snapshot = assistant_ledger.read_turn_status(db_path, "u1", turn_id)
    assert snapshot.state == "unknown"
    assert snapshot.proposal_operations == ({
        "surface": "review_undo",
        "operation_id": operation_id,
    },)


def test_completed_turn_replays_snapshot_without_reexecuting_agent(db_path):
    calls = 0

    class Agent:
        async def astream_run(self, _payload, *, scope):
            nonlocal calls
            calls += 1
            yield "完整回复"

    async def collect():
        return [event async for event in run_chat(
            "同一请求", "replay-session", "u1", client_turn_id="replay-turn",
            agent_factory=lambda _hooks: Agent(),
        )]

    first = run(collect())
    second = run(collect())
    assert calls == 1
    assert [event.event for event in second] == ["message_snapshot", "done"]
    assert second[0].data["text"] == "完整回复"
    assert first[-1].data["message_id"] == second[-1].data["message_id"]
    assert first[-1].data["replayed"] is False
    assert second[-1].data["replayed"] is True


def test_evicted_completed_turn_replays_safe_snapshot_without_reexecution(db_path):
    calls = 0

    class Agent:
        async def astream_run(self, _payload, *, scope):
            nonlocal calls
            calls += 1
            yield "只应生成一次"

    async def collect():
        return [event async for event in run_chat(
            "过期重取", "expired-session", "u1", client_turn_id="expired-turn",
            agent_factory=lambda _hooks: Agent(),
        )]

    first = run(collect())
    assert first[-1].event == "done" and calls == 1
    reference = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    expired_at = (reference - timedelta(days=31)).isoformat()
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE assistant_turns SET finished_time=?, updated_time=? "
            "WHERE user_id='u1' AND client_turn_id='expired-turn'",
            (expired_at, expired_at),
        )
    assert evict_expired_completed_replays(db_path, reference_time=reference) == 1

    retried = run(collect())
    assert calls == 1
    assert [event.event for event in retried] == ["message_snapshot", "done"]
    assert "超过 30 天的原回复内容已清理" in retried[0].data["text"]
    assert "这次重复请求不会再次执行" in retried[0].data["text"]
    assert retried[1].data["attachments"] == "consumed"
    assert retried[1].data["history_committed"] is True
    assert retried[1].data["replayed"] is True


def test_completion_publish_failure_never_sends_done(db_path, monkeypatch):
    from careerdesk.orchestration.assistant import service as assistant_module

    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "已经生成"

    monkeypatch.setattr(
        assistant_module.ledger,
        "complete_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )

    async def collect():
        return [event async for event in run_chat(
            "发布故障", "publish-session", "u1", client_turn_id="publish-turn",
            agent_factory=lambda _hooks: Agent(),
        )]

    events = run(collect())
    assert [event.event for event in events] == ["message_delta", "error"]
    assert events[-1].data["code"] == "turn_outcome_unknown"
    with read_connection(db_path) as conn:
        state = conn.execute(
            "SELECT state FROM assistant_turns WHERE user_id='u1' AND client_turn_id='publish-turn'"
        ).fetchone()
    assert state == ("unknown",)


def test_no_model_preflight_retains_image_and_does_not_claim_turn(db_path):
    image = user_upload_root(Path(db_path).parent, "chat", "u1") / "waiting.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nwaiting")

    async def collect():
        return [event async for event in run_chat(
            "配置后再看", "preflight-session", "u1", client_turn_id="preflight-turn",
            attachments=[{"kind": "image", "filename": "waiting.png", "stored": image.name}],
        )]

    events = run(collect())
    assert [event.event for event in events] == ["error"]
    assert events[0].data["code"] == "model_not_configured"
    assert image.exists()
    assert count(db_path, "SELECT COUNT(*) FROM assistant_turns") == 0


def test_standard_workbook_chat_import_bypasses_model_and_creates_preview(
    db_path,
    tmp_path,
):
    source = tmp_path / "jobs.csv"
    source.write_text(
        "公司名称,岗位名称,当前阶段\n星海科技,数据分析师,已投递\n",
        encoding="utf-8",
    )
    structured = parse_standard_workbook(source).structured_text

    async def collect(message: str, turn_id: str):
        return [event async for event in run_chat(
            message,
            "standard-workbook-session",
            "u1",
            client_turn_id=turn_id,
            attachments=[{
                "kind": "document",
                "filename": "jobs.csv",
                "text": structured,
                "truncated": False,
            }],
        )]

    events = run(collect(
        "请帮我批量导入这份表格中的岗位，并生成可核对的导入预览。",
        "00000000-0000-4000-8000-000000000401",
    ))

    assert [event.event for event in events] == [
        "tool_status", "tool_status", "message_delta", "done",
    ]
    assert events[0].data == {
        "tool": "parse_jobs",
        "label": "读取表格…",
    }
    assert events[1].data["proposal_surface"] == "intake"
    assert "已用本地代码读取标准表格" in events[2].data["text"]
    assert events[-1].data["proposal_operations"] == [{
        "surface": "intake",
        "operation_id": events[1].data["proposal_operation_id"],
    }]
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 0
    assert count(
        db_path,
        "SELECT COUNT(*) FROM journal WHERE kind='jd_batch' AND state='awaiting_user'",
    ) == 1

    non_import = run(collect(
        "请总结这份表格",
        "00000000-0000-4000-8000-000000000402",
    ))
    assert [event.event for event in non_import] == ["error"]
    assert non_import[0].data["code"] == "model_not_configured"


def test_english_standard_workbook_import_stays_local_and_english(db_path, tmp_path):
    source = tmp_path / "jobs.csv"
    source.write_text(
        "公司名称,岗位名称,当前阶段\n星海科技,数据分析师,已投递\n",
        encoding="utf-8",
    )
    structured = parse_standard_workbook(source).structured_text

    async def collect():
        return [event async for event in run_chat(
            "Import the roles in this workbook and prepare a preview.",
            "english-standard-workbook-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000403",
            output_locale="en",
            attachments=[{
                "kind": "document",
                "filename": "jobs.csv",
                "text": structured,
                "truncated": False,
            }],
        )]

    events = run(collect())

    assert [event.event for event in events] == [
        "tool_status", "tool_status", "message_delta", "done",
    ]
    assert events[0].data["label"] == "Reading workbook…"
    assert events[1].data["label"] == "The import preview is ready for your confirmation…"
    assert "The workbook was read locally" in events[2].data["text"]
    assert not any(
        "\u3400" <= character <= "\u9fff"
        for character in events[2].data["text"]
    )


def test_missing_model_key_fails_before_claim_and_same_turn_can_retry(
    db_path,
    monkeypatch,
):
    from agentmaker import LLMConfigError
    from careerdesk.orchestration.assistant import service as assistant_module
    from careerdesk.platform.ai import client as llm_client

    settings = SimpleNamespace(
        db_path=db_path,
        llm_model="openai",
        strict_offline=False,
        conversation_embedding_enabled=False,
    )
    monkeypatch.setattr(assistant_module, "get_settings", lambda: settings)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        llm_client,
        "build_llm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            LLMConfigError("OpenAI API Key 未配置")
        ),
    )

    async def collect():
        return [event async for event in run_chat(
            "修好配置后重试",
            "missing-key-session",
            "u1",
            client_turn_id="missing-key-turn",
        )]

    rejected = run(collect())
    assert [event.event for event in rejected] == ["error"]
    assert rejected[0].data["code"] == "model_not_configured"
    assert rejected[0].data["retryable"] is True
    assert rejected[0].data["history_committed"] is False
    assert "API Key" in rejected[0].data["message"]
    assert count(db_path, "SELECT COUNT(*) FROM assistant_turns") == 0

    request_llm = object()
    build_calls = []
    agent_llms = []

    def build_llm(_model, **_kwargs):
        build_calls.append(True)
        assert count(db_path, "SELECT COUNT(*) FROM assistant_turns") == 0
        return request_llm

    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "配置已恢复"

    def build_agent(
        _db_path,
        llm,
        _user_id,
        *,
        client_turn_id,
        review_supplement_reference,
        trusted_review_source,
        hooks,
        conversation_embedding_enabled,
        trace_path,
        resource_closers,
        proposal_recorder,
        output_locale,
    ):
        agent_llms.append(llm)
        assert client_turn_id == "missing-key-turn"
        assert review_supplement_reference is None
        assert trusted_review_source == "修好配置后重试"
        assert hooks and conversation_embedding_enabled is False
        assert trace_path == str(Path(db_path).parent / "traces.jsonl")
        assert len(resource_closers) == 1
        assert callable(proposal_recorder)
        assert output_locale == "zh-CN"
        return Agent()

    monkeypatch.setattr(llm_client, "build_llm", build_llm)
    monkeypatch.setattr("careerdesk.agentic.agents.build_career_assistant", build_agent)

    retried = run(collect())
    assert retried[-1].event == "done"
    assert build_calls == [True]
    assert agent_llms == [request_llm]
    assert count(db_path, "SELECT COUNT(*) FROM assistant_turns") == 1
    assert count(
        db_path,
        "SELECT COUNT(*) FROM assistant_turns WHERE state='completed'",
    ) == 1


def test_missing_model_capacity_reports_exact_recovery_before_claim(db_path, monkeypatch):
    from agentmaker import LLMConfigError
    from careerdesk.orchestration.assistant import service as assistant_module
    from careerdesk.platform.ai import client as llm_client
    from careerdesk.platform.ai.client import MODEL_CAPABILITY_MESSAGE

    settings = SimpleNamespace(
        db_path=db_path,
        llm_model="openai:custom-model",
        llm_context_window=None,
        llm_max_output_tokens=None,
        strict_offline=False,
        conversation_embedding_enabled=False,
    )
    monkeypatch.setattr(assistant_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        llm_client,
        "build_llm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            LLMConfigError(MODEL_CAPABILITY_MESSAGE)
        ),
    )

    async def collect():
        return [event async for event in run_chat(
            "容量补齐后重试",
            "capacity-session",
            "u1",
            client_turn_id="capacity-turn",
        )]

    events = run(collect())

    assert [event.event for event in events] == ["error"]
    assert events[0].data["code"] == "model_capabilities_missing"
    assert "context window" in events[0].data["message"]
    assert "max output tokens" in events[0].data["message"]
    assert events[0].data["retryable"] is True
    assert count(db_path, "SELECT COUNT(*) FROM assistant_turns") == 0


def test_unknown_provider_fails_before_assistant_turn_claim(db_path, monkeypatch):
    from careerdesk.orchestration.assistant import service as assistant_module

    monkeypatch.setattr(
        assistant_module,
        "get_settings",
        lambda: SimpleNamespace(
            db_path=db_path,
            llm_model="unknown-provider:model",
            strict_offline=False,
        ),
    )

    async def collect():
        return [event async for event in run_chat(
            "未知供应商",
            "unknown-provider-session",
            "u1",
            client_turn_id="unknown-provider-turn",
        )]

    events = run(collect())
    assert [(event.event, event.data["code"]) for event in events] == [
        ("error", "model_not_configured"),
    ]
    assert events[0].data["retryable"] is True
    assert count(db_path, "SELECT COUNT(*) FROM assistant_turns") == 0


def test_strict_local_adapter_drift_fails_before_assistant_turn_claim(
    db_path,
    monkeypatch,
):
    from careerdesk.orchestration.assistant import service as assistant_module
    from careerdesk.platform.ai import client as llm_client

    class DriftedClient:
        _adapter = object()

    monkeypatch.setattr(
        assistant_module,
        "get_settings",
        lambda: SimpleNamespace(
            db_path=db_path,
            llm_model="ollama:qwen3",
            strict_offline=True,
        ),
    )
    monkeypatch.setattr(llm_client, "LLMClient", lambda *_args, **_kwargs: DriftedClient())

    async def collect():
        return [event async for event in run_chat(
            "严格离线 adapter 漂移",
            "adapter-drift-session",
            "u1",
            client_turn_id="adapter-drift-turn",
        )]

    events = run(collect())
    assert [(event.event, event.data["code"]) for event in events] == [
        ("error", "model_not_configured"),
    ]
    assert events[0].data["retryable"] is True
    assert count(db_path, "SELECT COUNT(*) FROM assistant_turns") == 0


@pytest.mark.parametrize("state", ["completed", "running", "unknown"])
def test_existing_default_turn_semantics_precede_model_preflight(
    db_path,
    monkeypatch,
    state,
):
    from careerdesk.orchestration.assistant import service as assistant_module
    from careerdesk.platform.ai import client as llm_client

    message = "已有 durable 状态"
    session_id = f"existing-{state}-session"
    turn_id = f"existing-{state}-turn"
    digest = assistant_ledger.chat_request_hash(session_id, message, [])
    claimed = assistant_ledger.claim_turn(db_path, "u1", turn_id, session_id, digest)
    assert claimed.status == "execute" and claimed.attempt_token
    if state == "completed":
        assistant_ledger.complete_turn(
            db_path,
            "u1",
            turn_id,
            claimed.attempt_token,
            [
                {"event": "message_snapshot", "data": {"text": "稳定回复"}},
                {
                    "event": "done",
                    "data": {
                        "session": session_id,
                        "message_id": "durable-message",
                        "client_turn_id": turn_id,
                        "history_committed": True,
                        "attachments": "consumed",
                        "replayed": True,
                    },
                },
            ],
        )
    elif state == "unknown":
        assistant_ledger.mark_turn_unknown(
            db_path,
            "u1",
            turn_id,
            claimed.attempt_token,
            {
                "code": "turn_outcome_unknown",
                "message": "已有不确定结果",
                "retryable": False,
            },
        )

    monkeypatch.setattr(
        assistant_module,
        "get_settings",
        lambda: SimpleNamespace(
            db_path=db_path,
            llm_model="openai:gpt-4o-mini",
            strict_offline=False,
        ),
    )

    def forbidden_build(*_args, **_kwargs):
        raise AssertionError("existing durable turn must not build an LLM")

    monkeypatch.setattr(llm_client, "build_llm", forbidden_build)

    async def collect():
        return [event async for event in run_chat(
            message,
            session_id,
            "u1",
            client_turn_id=turn_id,
        )]

    events = run(collect())
    if state == "completed":
        assert [event.event for event in events] == ["message_snapshot", "done"]
        assert events[0].data["text"] == "稳定回复"
    elif state == "running":
        assert [event.event for event in events] == ["error"]
        assert events[0].data["code"] == "turn_in_progress"
        assert events[0].data["retryable"] is True
    else:
        assert [event.event for event in events] == ["error"]
        assert events[0].data["code"] == "turn_outcome_unknown"
        assert events[0].data["retryable"] is False


def test_run_chat_removes_consumed_image_after_success(db_path, monkeypatch):
    from careerdesk.orchestration.assistant import service as assistant_module

    uploads = user_upload_root(Path(db_path).parent, "chat", "u1")
    uploads.mkdir(parents=True, exist_ok=True)
    image = uploads / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ninlineable")
    monkeypatch.setattr(
        assistant_module, "get_settings",
        lambda: SimpleNamespace(db_path=db_path, llm_model="vision-test"),
    )
    monkeypatch.setattr(
        "careerdesk.platform.ai.client.supports_image_input",
        lambda _model, **_kwargs: (True, ""),
    )
    calls = []

    class Agent:
        async def astream_run(self, payload, *, scope):
            calls.append(True)
            assert isinstance(payload, list) and payload[0]["type"] == "image"
            yield "看到了"

    async def collect():
        return [event async for event in run_chat(
            "请看图", "image-session", "u1", client_turn_id="image-turn",
            attachments=[{"kind": "image", "filename": "photo.png", "stored": image.name}],
            agent_factory=lambda _hooks: Agent(),
        )]

    events = run(collect())
    assert events[-1].event == "done"
    assert not image.exists()
    replayed = run(collect())
    assert calls == [True]
    assert [event.event for event in replayed] == ["message_snapshot", "done"]


def test_same_session_requests_are_serialized_across_agent_instances(db_path):
    active = 0
    max_active = 0

    def factory(_hooks):
        class Agent:
            async def astream_run(self, _payload, *, scope):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                try:
                    await asyncio.sleep(0.02)
                    yield scope.session
                finally:
                    active -= 1

        return Agent()

    async def scenario():
        async def collect(turn_id: str):
            return [event async for event in run_chat(
                "同会话", "same-session", "u1", client_turn_id=turn_id,
                agent_factory=factory,
            )]

        return await asyncio.gather(collect("turn-a"), collect("turn-b"))

    streams = run(scenario())
    assert max_active == 1
    assert sorted(stream[-1].event for stream in streams) == ["done", "error"]
    rejected = next(stream[-1] for stream in streams if stream[-1].event == "error")
    assert rejected.data["code"] == "session_busy"


def test_disconnect_waits_for_agent_before_releasing_fence(db_path):
    started = asyncio.Event()
    finalized = asyncio.Event()
    release = asyncio.Event()

    class Agent:
        async def astream_run(self, _payload, *, scope):
            started.set()
            try:
                await release.wait()
                yield scope.session
            finally:
                finalized.set()

    async def scenario():
        async def consume():
            async for _ in run_chat(
                "会断开", "cancel-session", "u1", client_turn_id="cancel-turn",
                agent_factory=lambda _hooks: Agent(),
            ):
                pass

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with read_connection(db_path) as conn:
            state = conn.execute(
                "SELECT state FROM assistant_turns "
                "WHERE user_id='u1' AND client_turn_id='cancel-turn'"
            ).fetchone()
        assert state == ("running",)

        release.set()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finalized.wait(), timeout=1)
        await asyncio.sleep(0)
        orphaned_queue_gets = [
            candidate for candidate in asyncio.all_tasks()
            if candidate is not asyncio.current_task()
            and not candidate.done()
            and getattr(candidate.get_coro(), "__qualname__", "") == "Queue.get"
        ]
        assert orphaned_queue_gets == []

    run(scenario())
    with read_connection(db_path) as conn:
        state = conn.execute(
            "SELECT state FROM assistant_turns WHERE user_id='u1' AND client_turn_id='cancel-turn'"
        ).fetchone()
    assert state == ("unknown",)

    called = []

    async def retry():
        return [event async for event in run_chat(
            "会断开", "cancel-session", "u1", client_turn_id="cancel-turn",
            agent_factory=lambda _hooks: called.append(True),
        )]

    retried = run(retry())
    assert called == []
    assert [event.event for event in retried] == ["error"]
    assert retried[0].data["code"] == "turn_outcome_unknown"
    assert "history_committed" not in retried[0].data


def test_cancel_resistant_agent_keeps_session_fenced_until_stopped(db_path):
    thread_started = threading.Event()
    thread_finished = threading.Event()
    release_thread = threading.Event()

    class Agent:
        async def astream_run(self, _payload, *, scope):
            def blocking_tool():
                thread_started.set()
                release_thread.wait(timeout=2)
                thread_finished.set()

            await asyncio.to_thread(blocking_tool)
            yield scope.session

    async def scenario():
        async def consume():
            async for _ in run_chat(
                "延迟取消", "resistant-session", "u1", client_turn_id="resistant-turn",
                agent_factory=lambda _hooks: Agent(),
            ):
                pass

        consumer = asyncio.create_task(consume())
        assert await asyncio.to_thread(thread_started.wait, 1)
        consumer.cancel()
        await asyncio.sleep(0)

        assert not consumer.done()
        assert not thread_finished.is_set()
        with read_connection(db_path) as conn:
            state = conn.execute(
                "SELECT state FROM assistant_turns "
                "WHERE user_id='u1' AND client_turn_id='resistant-turn'"
            ).fetchone()
        assert state == ("running",)

        second_agent_called = False

        def forbidden_factory(_hooks):
            nonlocal second_agent_called
            second_agent_called = True
            raise AssertionError("同 session 的第二个 Agent 不得在旧执行退出前启动")

        blocked = [event async for event in run_chat(
            "新一轮", "resistant-session", "u1", client_turn_id="blocked-turn",
            agent_factory=forbidden_factory,
        )]
        assert second_agent_called is False
        assert [event.event for event in blocked] == ["error"]
        assert blocked[0].data["code"] == "session_busy"

        consumer.cancel()
        await asyncio.sleep(0)
        assert not consumer.done()

        release_thread.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(consumer, timeout=1)
        assert thread_finished.is_set()

        with read_connection(db_path) as conn:
            state = conn.execute(
                "SELECT state FROM assistant_turns "
                "WHERE user_id='u1' AND client_turn_id='resistant-turn'"
            ).fetchone()
        assert state == ("unknown",)

        class NextAgent:
            async def astream_run(self, _payload, *, scope):
                yield scope.session

        after = [event async for event in run_chat(
            "安全的新一轮", "resistant-session", "u1", client_turn_id="after-turn",
            agent_factory=lambda _hooks: NextAgent(),
        )]
        assert after[-1].event == "done"

    run(scenario())


def test_run_limit_is_incomplete_error_not_done(db_path):
    from agentmaker import RunLimitExceeded

    class Agent:
        async def astream_run(self, _payload, *, scope):
            if False:
                yield scope.session
            raise RunLimitExceeded("secret internal limit")

    async def collect():
        return [event async for event in run_chat(
            "太长", "limited", "u1", client_turn_id="limited-turn",
            agent_factory=lambda _hooks: Agent(),
        )]

    events = run(collect())
    assert [event.event for event in events] == ["error"]
    assert events[0].data["code"] == "turn_outcome_unknown"
    assert events[0].data["retryable"] is False
    assert "history_committed" not in events[0].data
    assert "secret" not in events[0].data["message"]


def test_agent_setup_failure_reports_known_no_execution_and_safe_retry(db_path):
    def broken_factory(_hooks):
        raise RuntimeError("missing /private/frozen/sqlite_vec/vec0.dylib")

    async def collect():
        return [event async for event in run_chat(
            "测试安装包", "setup-session", "u1", client_turn_id="setup-turn",
            agent_factory=broken_factory,
        )]

    events = run(collect())
    assert [event.event for event in events] == ["error"]
    assert events[0].data["code"] == "assistant_setup_failed"
    assert events[0].data["retryable"] is True
    assert events[0].data["history_committed"] is False
    assert "尚未调用模型或工具" in events[0].data["message"]
    assert "/private" not in events[0].data["message"]

    replayed = run(collect())
    assert replayed[0].data["code"] == "assistant_setup_failed"
    assert replayed[0].data["history_committed"] is False


def test_unknown_tool_status_is_not_shown(db_path):
    captured_hooks = []

    class Agent:
        async def astream_run(self, _payload, *, scope):
            captured_hooks[0].before_tool("internal_experimental_tool", {})
            yield "完成"

    def factory(hooks):
        captured_hooks.extend(hooks)
        return Agent()

    async def collect():
        return [event async for event in run_chat(
            "测试", "tool-label", "u1", client_turn_id="tool-label-turn",
            agent_factory=factory,
        )]

    events = run(collect())
    assert not any(event.event == "tool_status" for event in events)
    assert "".join(
        event.data["text"] for event in events if event.event == "message_delta"
    ) == "完成"


@pytest.mark.parametrize("fails", [False, True])
def test_default_agent_request_resources_close_on_success_and_error(db_path, monkeypatch, fails):
    from careerdesk.orchestration.assistant import service as assistant_module

    closed = []
    turn_id = "5ba1c2e2-293b-42fd-81b5-2789bc0c30d7"

    class Agent:
        async def astream_run(self, _payload, *, scope):
            if fails:
                raise RuntimeError("model failed")
            yield scope.session

    class OwnedLLM:
        async def aclose(self):
            closed.append("llm")

    def build_agent(
        _db_path,
        _llm,
        _user_id,
        *,
        client_turn_id,
        review_supplement_reference,
        trusted_review_source,
        hooks,
        conversation_embedding_enabled,
        trace_path,
        resource_closers,
        proposal_recorder,
        output_locale,
    ):
        assert hooks and len(resource_closers) == 1
        assert callable(proposal_recorder)
        assert conversation_embedding_enabled is False
        assert trace_path == str(Path(db_path).parent / "traces.jsonl")
        assert client_turn_id == turn_id
        assert review_supplement_reference is None
        assert trusted_review_source == "测试"
        assert output_locale == "zh-CN"
        resource_closers.append(lambda: closed.append("agent"))
        return Agent()

    monkeypatch.setattr(
        assistant_module, "get_settings",
        lambda: SimpleNamespace(db_path=db_path, llm_model="test:model"),
    )
    monkeypatch.setattr("careerdesk.agentic.agents.build_career_assistant", build_agent)
    monkeypatch.setattr(
        "careerdesk.platform.ai.client.build_llm",
        lambda _model, **_kwargs: OwnedLLM(),
    )

    async def collect():
        return [event async for event in run_chat(
            "测试", "resource", "u1", client_turn_id=turn_id,
        )]

    if fails:
        events = run(collect())
        assert events[-1].event == "error"
        assert events[-1].data["code"] == "turn_outcome_unknown"
    else:
        assert run(collect())[-1].event == "done"
    assert closed == ["agent", "llm"]


def test_resource_close_failure_does_not_create_done_then_error(db_path, monkeypatch):
    from careerdesk.orchestration.assistant import service as assistant_module

    closed = []
    turn_id = "1fb41931-d8a4-43ef-8cf9-da85c605a573"

    class Agent:
        async def astream_run(self, _payload, *, scope):
            yield "完成"

    def fail_close():
        closed.append("failed")
        raise OSError("close failed")

    def build_agent(
        _db_path,
        _llm,
        _user_id,
        *,
        client_turn_id,
        review_supplement_reference,
        trusted_review_source,
        hooks,
        conversation_embedding_enabled,
        trace_path,
        resource_closers,
        proposal_recorder,
        output_locale,
    ):
        assert client_turn_id == turn_id
        assert review_supplement_reference is None
        assert trusted_review_source == "测试清理"
        assert conversation_embedding_enabled is False
        assert trace_path == str(Path(db_path).parent / "traces.jsonl")
        assert callable(proposal_recorder)
        assert output_locale == "zh-CN"
        resource_closers.extend([lambda: closed.append("ok"), fail_close])
        return Agent()

    monkeypatch.setattr(
        assistant_module, "get_settings",
        lambda: SimpleNamespace(db_path=db_path, llm_model="test:model"),
    )
    monkeypatch.setattr("careerdesk.agentic.agents.build_career_assistant", build_agent)
    monkeypatch.setattr(
        "careerdesk.platform.ai.client.build_llm",
        lambda _model, **_kwargs: object(),
    )

    async def collect():
        return [event async for event in run_chat(
            "测试清理", "cleanup-session", "u1", client_turn_id=turn_id,
        )]

    events = run(collect())
    assert closed == ["failed", "ok"]
    assert [event.event for event in events if event.event in {"done", "error"}] == ["done"]


def test_conversation_search_recalls_across_sessions(db_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from agentmaker import Message
    from careerdesk.agentic.memory import build_conversation_memory
    from careerdesk.agentic.tools import ConversationSearchTool

    closers = []
    conversation, _ = build_conversation_memory(
        db_path,
        embedding_enabled=False,
        user_id="u1",
        resource_closers=closers,
    )
    conversation.append(Message(role="user", content="记住：我只投后端岗，不投前端"),
                        scope=Scope(user="u1", app="careerdesk", session="s1"))
    conversation.append(Message(role="user", content="今天天气不错"),
                        scope=Scope(user="u1", app="careerdesk", session="s2"))

    tool = ConversationSearchTool(db_path, conversation, "u1")
    response = tool.run({"query": "后端 前端"})
    assert response.status == "success" and "只投后端" in response.text
    assert response.data["items"][0]["session_id"] == "s1"
    assert "user" in {turn["role"] for turn in response.data["items"][0]["turn_context"]}
    other = ConversationSearchTool(db_path, conversation, "u2")
    assert "只投后端" not in other.run({"query": "后端 前端"}).text
    for close in reversed(closers):
        close()


def test_career_assistant_upgrades_with_embedding_key(db_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key-alone-is-not-consent")
    plain = build_career_assistant(
        db_path,
        ScriptedLLM([]),
        "u1",
        client_turn_id=AGENT_TURN_ID,
        trusted_review_source="测试请求",
    )
    plain_names = [tool.name for tool in plain.tool_registry.list_tools()]
    read_tools = {"query_timeline", "query_study", "query_library", "query_grill", "query_prep",
                  "query_status", "preferences"}
    assert read_tools <= set(plain_names) and "manage_review" in plain_names
    assert "load_skill" in plain_names
    assert len(plain_names) == 15 and "conversation_search" in plain_names
    conversation_tool = plain.tool_registry.get("conversation_search")
    assert conversation_tool.origin == "careerdesk"
    assert conversation_tool.external_content is True
    assert conversation_tool.supports_parallel is True
    assert {parameter.name for parameter in conversation_tool.get_parameters()} == {
        "query", "date_from", "date_to", "session_id", "roles", "top_k",
    }
    assert "emotional-support" in plain.system_prompt

    monkeypatch.delenv("OPENAI_API_KEY")
    allowed_without_key = build_career_assistant(
        db_path,
        ScriptedLLM([]),
        "u1",
        client_turn_id=AGENT_TURN_ID,
        trusted_review_source="测试请求",
        conversation_embedding_enabled=True,
    )
    assert "conversation_search" in {
        tool.name for tool in allowed_without_key.tool_registry.list_tools()
    }

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-hermetic")
    upgraded = build_career_assistant(
        db_path,
        ScriptedLLM([]),
        "u1",
        client_turn_id=AGENT_TURN_ID,
        trusted_review_source="测试请求",
        conversation_embedding_enabled=True,
    )
    names = [tool.name for tool in upgraded.tool_registry.list_tools()]
    assert "conversation_search" in names and "preferences" in names and len(names) == 15
    assert "conversation_search" in upgraded.system_prompt


def test_career_assistant_injects_current_preferences_across_new_sessions(db_path):
    execute_preference_update_operation(
        db_path,
        "u1",
        operation_id="00000000-0000-4000-8000-000000000301",
        client_turn_id="00000000-0000-4000-8000-000000000302",
        changes=[
            {"op": "set", "key": "response_greeting", "value": "开头称呼亲爱的"},
            {"op": "set", "key": "response_tone", "value": "采用御姐风"},
            {"op": "set", "key": "投递方向", "value": "只投 Agent 应用"},
        ],
    )

    first = build_career_assistant(
        db_path,
        ScriptedLLM(["亲爱的，短回复。"], context_window=16_384),
        "u1",
        client_turn_id=AGENT_TURN_ID,
        trusted_review_source="测试请求",
    )
    second = build_career_assistant(
        db_path,
        ScriptedLLM([]),
        "u1",
        client_turn_id="00000000-0000-4000-8000-000000000303",
        trusted_review_source="测试请求",
    )

    for agent in (first, second):
        assert '"key":"response_greeting"' in agent.system_prompt
        assert '"key":"response_tone"' in agent.system_prompt
        assert "只投 Agent 应用" not in agent.system_prompt
        assert "不能覆盖上面的安全" in agent.system_prompt
    assert run(first.arun(
        "你好",
        scope=Scope(user="u1", app="careerdesk", session="preference-session"),
    )).final_output == "亲爱的，短回复。"


def test_preferences_tool_roundtrip_across_sessions(db_path):
    saver = PreferencesTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000211",
    )
    assert saver.run({
        "action": "apply",
        "changes": [{"op": "set", "key": "投递方向", "value": "只投大模型应用"}],
    }).status == "success"
    reader = PreferencesTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000212",
    )
    listed = reader.run({"action": "list"})
    assert "只投大模型应用" in listed.text
    assert listed.data["items"][0]["value"] == "只投大模型应用"

    updater = PreferencesTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000213",
    )
    updater.run({
        "action": "apply",
        "changes": [{"op": "set", "key": "投递方向", "value": "大模型应用优先，基础架构也可"}],
    })
    assert reader.run({"action": "list"}).data["items"][0]["value"].startswith("大模型应用优先")
    other = PreferencesTool(
        db_path, "u2", client_turn_id="00000000-0000-4000-8000-000000000214",
    )
    assert other.run({"action": "list"}).data is None
    deleter = PreferencesTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000215",
    )
    deleter.run({
        "action": "apply",
        "changes": [{"op": "delete", "key": "投递方向"}],
    })
    assert reader.run({"action": "list"}).data is None


def test_agent_saves_preference_via_tool(db_path):
    llm = ScriptedLLM([
        ScriptedLLM.tool_call("preferences", {
            "action": "apply",
            "changes": [{"op": "set", "key": "目标城市", "value": "杭州、深圳"}],
        }),
        "记好了：目标城市杭州、深圳。",
    ])
    agent = build_career_assistant(
        db_path,
        llm,
        "u1",
        client_turn_id=AGENT_TURN_ID,
        trusted_review_source="记住我只看杭州和深圳的岗位",
    )
    result = run(agent.arun("记住我只看杭州和深圳的岗位", scope=Scope(user="u1", app="careerdesk", session="s1")))
    assert "记好了" in result.final_output
    preferences = PreferencesTool(
        db_path, "u1", client_turn_id="00000000-0000-4000-8000-000000000216",
    )
    assert preferences.run({"action": "list"}).data["items"][0]["value"] == "杭州、深圳"


def test_history_compactor_trigger_scales_with_model_window(db_path):
    from careerdesk.agentic.agents.career_assistant.policy import (
        COMPACT_WINDOW_RATIO,
        build_history_compactor,
    )

    big = build_history_compactor(ScriptedLLM([], context_window=1_000_000))
    assert big.trigger_tokens == int(1_000_000 * COMPACT_WINDOW_RATIO)

    small = build_history_compactor(ScriptedLLM([], context_window=8_192))
    assert small.trigger_tokens == int(8_192 * COMPACT_WINDOW_RATIO)
    assert small.trigger_tokens < big.trigger_tokens

    assert build_history_compactor(ScriptedLLM([], context_window=None)) is None

    english = build_history_compactor(
        ScriptedLLM([], context_window=8_192),
        "en",
    )
    assert not any(
        "\u3400" <= character <= "\u9fff"
        for character in english.summary_prompt
    )


def test_output_locale_guardrail_blocks_only_obvious_whole_answer_mismatches():
    english = _OutputLocaleGuardrail("en")
    chinese = _OutputLocaleGuardrail("zh-CN")

    assert english.check(
        "I am CareerDesk's career assistant. I help with applications and interviews."
    ).passed
    assert not english.check(
        "我是 CareerDesk 的求职助手，可以帮助你管理投递、准备面试、练习题目和整理复盘。"
    ).passed
    assert english.check(
        "The résumé says ‘负责推荐系统与数据平台开发’, which supports this role. "
        "I would keep the original wording as a quotation and explain the evidence in English."
    ).passed
    assert chinese.check("我是 CareerDesk 的求职助手，可以帮你管理投递和面试准备。").passed
    assert not chinese.check(
        "I am CareerDesk's career assistant. I can help you track applications, prepare for "
        "interviews, practise questions, and review your progress whenever you need it."
    ).passed


def test_production_english_chat_never_publishes_an_obvious_chinese_reply(
    db_path,
    monkeypatch,
):
    from careerdesk.orchestration.assistant import service as assistant_module

    settings = SimpleNamespace(
        db_path=db_path,
        llm_model="test:model",
        strict_offline=False,
        conversation_embedding_enabled=False,
    )
    model = ScriptedLLM([
        "我是 CareerDesk 的求职助手，可以帮助你管理投递、准备面试、练习题目和整理复盘。",
    ], context_window=32_768)
    monkeypatch.setattr(assistant_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "careerdesk.platform.ai.client.build_llm",
        lambda _model, **_kwargs: model,
    )

    async def collect():
        return [event async for event in run_chat(
            "Who are you?",
            "english-language-guard-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000404",
            output_locale="en",
        )]

    events = run(collect())
    reply = rendered_chat_text(events)
    assert events[-1].event == "done"
    assert reply.startswith("I could not produce a reliable answer")
    assert not any("\u3400" <= character <= "\u9fff" for character in reply)


def test_late_stream_guard_failure_does_not_duplicate_production_history(
    db_path,
    monkeypatch,
):
    from careerdesk.orchestration.assistant import service as assistant_module

    settings = SimpleNamespace(
        db_path=db_path,
        llm_model="test:model",
        strict_offline=False,
        conversation_embedding_enabled=False,
    )
    model = ScriptedLLM([
        ScriptedLLM.tool_call(
            "preferences",
            {"action": "list"},
            content="我已经更新了这个岗位。",
        ),
        "实际上没有执行任何修改。",
    ])
    monkeypatch.setattr(assistant_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "careerdesk.platform.ai.client.build_llm",
        lambda _model, **_kwargs: model,
    )

    async def collect():
        return [event async for event in run_chat(
            "只查询当前信息",
            "late-stream-guard-session",
            "u1",
            client_turn_id="00000000-0000-4000-8000-000000000410",
        )]

    events = run(collect())

    assert rendered_chat_text(events).startswith("这次没有取得")
    with read_connection(db_path) as conn:
        history = conn.execute(
            "SELECT role, content FROM session_messages "
            "WHERE sc_user='u1' AND sc_session='late-stream-guard-session' ORDER BY rowid",
        ).fetchall()
    assert history == [("user", "只查询当前信息"), ("assistant", "实际上没有执行任何修改。")]


def test_main_agent_attaches_compactor_without_breaking_short_chat(db_path):
    agent = build_career_assistant(
        db_path,
        ScriptedLLM(["短对话直接回。"], context_window=32_768),
        "u1",
        client_turn_id=AGENT_TURN_ID,
        trusted_review_source="你好",
    )
    assert agent.harness.compactor is not None
    result = run(agent.arun("你好", scope=Scope(user="u1", app="careerdesk", session="s1")))
    assert result.final_output == "短对话直接回。"
