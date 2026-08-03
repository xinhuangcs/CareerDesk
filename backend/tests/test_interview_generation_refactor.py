"""Integration coverage for the immutable interview-set architecture."""

import asyncio

from types import SimpleNamespace
from uuid import uuid4

import pytest

from careerdesk.features.applications import repository as applications
from careerdesk.features.grill import repository as grill
from careerdesk.features.grill import service as grill_service
from careerdesk.features.grill.ai_models import JudgeVerdict
from careerdesk.features.questions import repository as catalogue
from careerdesk.features.questions import sets
from careerdesk.features.resumes.repository import upsert_resume
from careerdesk.orchestration.interview_generation.contracts import (
    GenerateQuestionSetRequest, StartInterviewSessionRequest,
)
from careerdesk.orchestration.interview_generation import ai_tasks, api as interview_api
from careerdesk.orchestration.interview_generation.workflow import (
    InterviewGenerationWorkflow, capacity_for, current_policy_fingerprint, freeze_input,
    start_current_session,
)
from careerdesk.features.questions.generation_models import GeneratedQuestionSet
from careerdesk.platform.database import init_db, now_iso, transaction
from tests.support.question_sets import seed_question_set


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    return path


def _real_question(db_path: str, user_id: str = "u1") -> int:
    with transaction(db_path) as conn:
        return conn.execute(
            "INSERT INTO questions (user_id, text, source, category, channel, "
            "response_format, evaluation_kind, primary_competency, rubric_json, answer_guide_json, "
            "created_time, updated_time) VALUES (?, '请介绍一次困难决策', 'real', "
            "'behavioral_situational', 'interview', 'oral_text', 'rubric', '决策能力', "
            "'{\"essential_criteria\":[\"切题且具体\"],\"quality_signals\":[],\"critical_errors\":[]}', "
            "'{\"kind\":\"guide\",\"text\":\"说明背景、取舍与结果\"}', ?, ?)",
            (user_id, now_iso(), now_iso()),
        ).lastrowid


def test_historical_library_snapshot_remains_readable_but_cannot_start(db_path):
    question_id = _real_question(db_path)
    set_id = seed_question_set(db_path, "u1", [question_id], kind="library_snapshot")
    frozen = sets.get_question_set(db_path, "u1", set_id, include_items=True)
    assert frozen["state"] == "ready"
    assert frozen["items"][0]["text"] == "请介绍一次困难决策"

    with transaction(db_path) as conn:
        conn.execute("UPDATE questions SET text = '后来修改的题面' WHERE id = ?", (question_id,))
    with pytest.raises(ValueError, match="历史题集仅供查看"):
        start_current_session(db_path, "u1", question_set_id=set_id, question_count=5)


def test_basic_and_custom_freeze_exact_material_receipts(db_path):
    resume_id = upsert_resume(db_path, "u1", "通用简历", "第一段\r\n第二段")
    basic = freeze_input(db_path, "u1", edition="basic", resume_id=resume_id)
    assert basic.material_claim["resume_hash"]
    assert "\r" not in basic.materials[0]["segments"][0]["text"]

    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications (user_id, company, position, jd_text, resume_id, "
            "created_time, updated_time) VALUES ('u1', '示例公司', '产品经理', "
            "'负责访谈、分析和路线规划', ?, ?, ?)",
            (resume_id, now_iso(), now_iso()),
        ).lastrowid
    custom = freeze_input(db_path, "u1", edition="custom", application_id=application_id)
    assert custom.resume_id == resume_id
    assert {item["kind"] for item in custom.materials} == {"resume", "jd"}

    with transaction(db_path) as conn:
        conn.execute("UPDATE applications SET jd_text = '新的 JD' WHERE id = ?", (application_id,))
    updated = freeze_input(db_path, "u1", edition="custom", application_id=application_id)
    assert updated.material_fingerprint != custom.material_fingerprint


def test_generation_contract_rejects_removed_scope_fields():
    with pytest.raises(Exception):
        GenerateQuestionSetRequest.model_validate({
            "edition": "basic", "resume_id": 1, "unexpected_scope": "旧范围",
            "client_command_id": str(uuid4()),
        })
    with pytest.raises(Exception):
        GenerateQuestionSetRequest.model_validate({
            "edition": "custom", "application_id": 1, "include_research": True,
            "client_command_id": str(uuid4()),
        })
    assert all(
        route.path != "/api/interview-generation/library-snapshots"
        for route in interview_api.router.routes
    )


def test_session_contract_accepts_custom_positive_question_count():
    request = StartInterviewSessionRequest.model_validate({
        "question_set_id": 9,
        "question_count": 7,
    })
    assert request.question_count == 7
    with pytest.raises(Exception):
        StartInterviewSessionRequest.model_validate({
            "question_set_id": 9,
            "question_count": 0,
        })


@pytest.mark.parametrize(
    ("model", "expected_state"),
    [
        ("deepseek", "direct"),
        ("anthropic", "direct"),
        ("zhipu", "direct"),
        ("moonshot", "blocked"),
    ],
)
def test_readiness_resolves_default_provider_capacity(
    db_path,
    monkeypatch,
    model,
    expected_state,
):
    resume_id = upsert_resume(db_path, "u1", f"{model} 简历", "负责跨团队交付并复盘风险。")
    settings = SimpleNamespace(
        db_path=db_path,
        llm_model=model,
        llm_context_window=None,
        llm_max_output_tokens=None,
    )
    monkeypatch.setattr(interview_api, "get_settings", lambda: settings)

    result = interview_api.get_readiness(
        edition="basic",
        resume_id=resume_id,
        application_id=None,
        user_id="u1",
    )

    assert result["selection"]["capacity"]["state"] == expected_state
    if expected_state == "blocked":
        assert result["selection"]["capacity"]["code"] == "insufficient_model_capacity"
    else:
        assert result["selection"]["ready"] is True


def test_generation_workflow_resolves_default_capacity_and_custom_models_remain_closed(db_path):
    default_workflow = InterviewGenerationWorkflow(
        db_path,
        object(),
        model_label="deepseek",
        context_window=None,
        max_output_tokens=None,
    )
    assert default_workflow.context_window == 1_000_000
    assert default_workflow.max_output_tokens == 393_216

    custom_workflow = InterviewGenerationWorkflow(
        db_path,
        object(),
        model_label="openai:gpt-4o-mini",
        context_window=None,
        max_output_tokens=None,
    )
    assert capacity_for(
        ({"kind": "resume", "segments": []},),
        context_window=custom_workflow.context_window,
        max_output_tokens=custom_workflow.max_output_tokens,
    ) == {
        "state": "blocked",
        "code": "model_capacity_required",
        "effective_question_limit": 0,
        "compressed_materials": [],
        "extra_calls": 0,
    }


def test_generation_claim_returns_immediately_then_publishes_atomically(db_path, monkeypatch):
    resume_text = "负责跨团队交付，并复盘项目风险。"
    resume_id = upsert_resume(db_path, "u1", "面试简历", resume_text)
    output = GeneratedQuestionSet.model_validate({
        "questions": [{
            "text": f"请说明你如何识别并处理项目风险（场景 {index + 1}）？",
            "category": "resume_deep_dive", "channel": "interview",
            "response_format": "oral_text", "difficulty": "intermediate",
            "basis_kinds": ["resume"],
            "evidence_refs": [{"basis_kind": "resume", "ref_id": "R1"}],
            "limitations": [], "primary_competency": "风险管理", "secondary_tags": [],
            "evaluation_kind": "evidence_consistency",
            "rubric": {"essential_criteria": ["说明识别、行动和结果"],
                       "quality_signals": ["说明取舍"], "critical_errors": ["编造经历"]},
            "answer_guide": "先说明风险信号，再说明行动、取舍与结果。",
            "follow_up_allowed": True,
        } for index in range(3)],
        "coverage": {"processed_sources": ["resume"],
                     "covered_categories": ["resume_deep_dive"],
                     "omitted_categories": [], "omission_reasons": [], "limitations": []},
    })

    envelopes = []

    async def fake_generate(_llm, envelope, *, output_locale):
        assert output_locale == "zh-CN"
        envelopes.append(envelope)
        return output

    monkeypatch.setattr(ai_tasks, "generate_question_set", fake_generate)
    workflow = InterviewGenerationWorkflow(
        db_path, object(), model_label="fake", context_window=128_000,
        max_output_tokens=24_000,
    )

    async def exercise():
        claimed = await workflow.generate(
            "u1", edition="basic", resume_id=resume_id, application_id=None,
            client_command_id=str(uuid4()), refresh=False, enqueue_only=True,
        )
        assert claimed["status"] == "processing"
        assert sets.get_question_set(db_path, "u1", claimed["question_set_id"])["state"] == "running"
        completed = await workflow.run_pending()
        assert completed["status"] == "ready"
        published = sets.get_question_set(
            db_path, "u1", completed["question_set_id"], include_items=True,
        )
        assert published["content_locale"] == "zh-CN"
        assert published["input_receipt"]["content_locale"] == "zh-CN"
        assert published["question_count"] == 3
        assert published["items"][0]["answer_authority"] == "model_generated_unverified"
        assert published["items"][0]["evidence"][0]["excerpt"] == resume_text
        assert envelopes[0]["allowed_categories"] == [
            "hr_motivation", "resume_deep_dive", "behavioral_situational",
            "professional_domain", "case_work_sample",
        ]
        assert "target_question_count" not in envelopes[0]

    asyncio.run(exercise())


def test_capacity_allows_a_small_material_supported_set(db_path):
    resume_id = upsert_resume(db_path, "u1", "短简历", "负责交付。")
    frozen = freeze_input(db_path, "u1", edition="basic", resume_id=resume_id)

    result = capacity_for(
        frozen.materials,
        context_window=128_000,
        max_output_tokens=700,
    )

    assert result["state"] == "direct"
    assert result["effective_question_limit"] == 1


def test_unexpected_value_error_is_persisted_as_a_safe_generation_code(db_path, monkeypatch):
    resume_id = upsert_resume(db_path, "u1", "异常简历", "用于验证异常收口。")

    async def fail_with_programming_detail(*_args, **_kwargs):
        raise ValueError("private internal programming detail")

    monkeypatch.setattr(ai_tasks, "generate_question_set", fail_with_programming_detail)
    workflow = InterviewGenerationWorkflow(
        db_path, object(), model_label="fake", context_window=128_000,
        max_output_tokens=24_000,
    )

    result = asyncio.run(workflow.generate(
        "u1", edition="basic", resume_id=resume_id, application_id=None,
        client_command_id=str(uuid4()), refresh=False,
    ))

    assert result["code"] == "unexpected_generation_error"
    failed = sets.get_question_set(db_path, "u1", result["question_set_id"])
    assert failed["safe_error_code"] == "unexpected_generation_error"
    assert "private internal" not in result["message"]


def test_application_deletion_does_not_touch_frozen_sets(db_path):
    question_id = _real_question(db_path)
    referenced_set_id = seed_question_set(db_path, "u1", [question_id])
    session_id, _, _ = grill.create_session(
        db_path, "u1", question_set_id=referenced_set_id, question_count=5,
    )
    deletable_set_id = seed_question_set(db_path, "u1", [question_id])
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
            "VALUES ('u1', '独立公司', '独立岗位', ?, ?)",
            (now_iso(), now_iso()),
        ).lastrowid
        applications._delete_application_in_transaction(conn, "u1", application_id)

    assert sets.get_question_set(db_path, "u1", referenced_set_id) is not None
    assert grill.get_session(db_path, "u1", session_id) is not None
    assert sets.archive_or_delete_question_set(db_path, "u1", referenced_set_id) == "archived"
    assert sets.archive_or_delete_question_set(db_path, "u1", deletable_set_id) == "deleted"


def test_stale_generated_set_cannot_start_a_new_session(db_path):
    resume_id = upsert_resume(db_path, "u1", "会变化的简历", "第一版经历")
    question_id = _real_question(db_path)
    set_id = seed_question_set(db_path, "u1", [question_id])
    frozen = freeze_input(db_path, "u1", edition="basic", resume_id=resume_id)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE question_sets SET edition='basic', resume_id=?, "
            "material_fingerprint=?, policy_fingerprint=? WHERE id=?",
            (resume_id, frozen.material_fingerprint, current_policy_fingerprint(), set_id),
        )
    upsert_resume(db_path, "u1", "会变化的简历", "第二版经历", overwrite_existing=True)
    with pytest.raises(ValueError, match="材料已变化"):
        start_current_session(db_path, "u1", question_set_id=set_id, question_count=5)


def test_legacy_policy_set_cannot_start_a_new_session(db_path):
    resume_id = upsert_resume(db_path, "u1", "旧策略简历", "稳定经历")
    question_id = _real_question(db_path)
    set_id = seed_question_set(db_path, "u1", [question_id])
    frozen = freeze_input(db_path, "u1", edition="basic", resume_id=resume_id)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE question_sets SET edition='basic', resume_id=?, "
            "material_fingerprint=?, policy_fingerprint='retired-policy' WHERE id=?",
            (resume_id, frozen.material_fingerprint, set_id),
        )
    with pytest.raises(ValueError, match="策略版本已过期"):
        start_current_session(db_path, "u1", question_set_id=set_id, question_count=5)


def test_repeat_scope_controls_competency_identity(db_path, monkeypatch):
    resume_id = upsert_resume(db_path, "u1", "能力简历", "负责复杂决策")
    question_id = _real_question(db_path)
    set_id = seed_question_set(db_path, "u1", [question_id])
    frozen = freeze_input(db_path, "u1", edition="basic", resume_id=resume_id)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE question_sets SET edition='basic', resume_id=?, "
            "material_fingerprint=?, policy_fingerprint=? WHERE id=?",
            (resume_id, frozen.material_fingerprint, current_policy_fingerprint(), set_id),
        )
        conn.execute("UPDATE question_set_items SET repeat_scope='resume' WHERE question_set_id=?", (set_id,))
    session_id, question, _ = start_current_session(
        db_path, "u1", question_set_id=set_id, question_count=5,
    )

    async def fake_judge(*_args, **_kwargs):
        return JudgeVerdict(verdict="meets", stuck=False, strengths=["具体"], gaps=[],
                            next_step="保持", follow_up=None)

    monkeypatch.setattr(grill_service, "judge_answer", fake_judge)
    service = grill_service.GrillService(db_path, object())

    async def exercise():
        claimed = await service.answer(
            "u1", session_id, "具体回答", session_item_id=question["id"], enqueue_only=True,
        )
        assert claimed["status"] == "processing"
        assert (await service.run_pending_answer())["status"] == "finished"

    asyncio.run(exercise())
    progress = catalogue.competency_overview(db_path, "u1")
    assert progress["scopes"][0]["scope_kind"] == "resume"
    assert progress["scopes"][0]["scope_ref"] == str(resume_id)


def test_failed_generation_command_replays_terminal_error(db_path, monkeypatch):
    resume_id = upsert_resume(db_path, "u1", "失败简历", "用于失败重放")

    async def fail_generation(*_args, **_kwargs):
        raise ai_tasks.InterviewAITaskError("model_timeout")

    monkeypatch.setattr(ai_tasks, "generate_question_set", fail_generation)
    workflow = InterviewGenerationWorkflow(
        db_path, object(), model_label="fake", context_window=128_000,
        max_output_tokens=24_000,
    )
    command_id = str(uuid4())

    async def exercise():
        first = await workflow.generate(
            "u1", edition="basic", resume_id=resume_id, application_id=None,
            client_command_id=command_id, refresh=False,
        )
        assert first["status"] == "error"
        replay = await workflow.generate(
            "u1", edition="basic", resume_id=resume_id, application_id=None,
            client_command_id=command_id, refresh=False, enqueue_only=True,
        )
        assert replay["status"] == "error"
        assert replay["code"] == "model_timeout"

    asyncio.run(exercise())
