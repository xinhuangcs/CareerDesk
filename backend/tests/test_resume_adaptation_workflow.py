"""Resume adaptation workflow integration tests (all hermetic)."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.support import ScriptedLLM

from careerdesk.core.config import get_settings
from careerdesk.features.applications import public as applications
from careerdesk.features.research.public import build_research_snapshot, research_semantic_claim
from careerdesk.orchestration.application_prep.adaptation import (
    exact_text_segments,
    validate_and_materialize_report,
)
from careerdesk.orchestration.application_prep.adaptation_contracts import (
    ResumeAdaptationReport,
)
from careerdesk.orchestration.application_prep.adaptation_workflow import (
    ResumeAdaptationWorkflow,
)
from careerdesk.orchestration.application_prep.http_contracts import ResumeAdaptationResponse
from careerdesk.features.applications.contracts import ApplicationResumeBindingResponse
from careerdesk.platform.database import init_db, read_connection, transaction


NOW = datetime.now(timezone.utc).replace(microsecond=0)
USER = "adaptation-user"
JD = "负责可靠交付，并与跨职能团队协作。"
RESUME = "产品经理\n负责企业产品路线图，与研发和销售协作完成上线。\n"


class CapturingLLM(ScriptedLLM):
    def __init__(self, payloads: list[dict]):
        super().__init__([json.dumps(item, ensure_ascii=False) for item in payloads])
        self.requests: list[dict] = []

    async def chat(self, messages, *, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return await super().chat(messages, tools=tools, **kwargs)


def _reports() -> tuple[dict, dict]:
    company_source = [{
        "index": 1,
        "url": "https://example.com/company",
        "site": "example.com",
        "title": "公司公开资料",
        "date": NOW.date().isoformat(),
        "engines": ["fixture"],
    }, {
        "index": 2,
        "url": "https://reviews.example/company",
        "site": "reviews.example",
        "title": "员工评论",
        "date": NOW.date().isoformat(),
        "engines": ["fixture"],
    }]
    position_source = [{
        "index": 1,
        "url": "https://example.com/position",
        "site": "example.com",
        "title": "岗位公开资料",
        "date": NOW.date().isoformat(),
        "engines": ["fixture"],
    }]
    company = {
        "business": {"text": "提供企业软件。", "sources": [1]},
        "culture": {"text": "重视协作。", "sources": [1]},
        "recent_news": {"text": "近期发布新产品。", "sources": [1]},
        "interview_style": {"text": "关注事实证据。", "sources": [1]},
        "source_conflicts": [
            {
                "summary": "官网与员工评论对远程政策的描述不一致。",
                "sources": [1, 2],
            }
        ],
        "sources": company_source,
    }
    position = {
        "interview_process": {"text": "包含业务面试。", "sources": [1]},
        "experience_highlights": {"text": "重视交付范围。", "sources": [1]},
        "team_tech_clues": {"text": "跨职能团队。", "sources": [1]},
        "source_conflicts": [],
        "sources": position_source,
    }
    return company, position


def _research_prep(*, attempt_state: str = "succeeded", include_snapshot: bool = True) -> dict:
    claim = research_semantic_claim(
        company="示例公司",
        aliases=[],
        notes=None,
        department="产品",
        position="产品经理",
        jd_text=JD,
    )
    prep: dict = {
        "research_attempt": {
            "attempt_state": attempt_state,
            "generation": None,
            "updated_time": NOW.isoformat(),
            "error_code": None,
        },
    }
    if include_snapshot:
        company, position = _reports()
        prep["research_snapshot"] = build_research_snapshot(
            company_report=company,
            position_report=position,
            semantic_claim=claim,
            company_report_generated_time=NOW,
            position_report_generated_time=NOW,
            snapshot_id="0123456789abcdef0123456789abcdef",
        )
    return prep


def _seed(
    db_path: str,
    *,
    bound: bool = True,
    prep: dict | None = None,
    resume_text: str | None = RESUME,
) -> tuple[int, int]:
    init_db(db_path)
    with transaction(db_path) as conn:
        resume_id = conn.execute(
            "INSERT INTO resumes "
            "(user_id, name, content_text, content_hash, extraction_receipt_json, "
            "segments_json, binding, archived, created_time, updated_time) "
            "VALUES (?, '产品岗位版', ?, ?, '{}', '[]', 'family', 0, ?, ?)",
            (USER, resume_text, hashlib.sha256(resume_text.encode()).hexdigest(),
             NOW.isoformat(), NOW.isoformat()),
        ).lastrowid
        application_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, department, jd_text, jd_parsed_json, "
            "resume_id, prep_status, prep_json, created_time, updated_time) "
            "VALUES (?, '示例公司', '产品经理', '产品', ?, '{\"skills\":[\"协作\"]}', "
            "?, 'ready', ?, ?, ?)",
            (
                USER,
                JD,
                resume_id if bound else None,
                json.dumps(prep if prep is not None else _research_prep(), ensure_ascii=False),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        ).lastrowid
    return application_id, resume_id


def _report(*, bad_ref: bool = False) -> dict:
    jd_ref = exact_text_segments(JD, namespace="J")[0].segment_id
    resume_ref = exact_text_segments(RESUME, namespace="R")[0].segment_id
    if bad_ref:
        jd_ref = "J1-9999-deadbeef"
    return {
        "mode": "full",
        "fit_band": "promising",
        "summary_sentences": ["现有跨职能交付经历与岗位具备可迁移契合点。"],
        "requirement_assessments": [{
            "requirement_summary": "可靠交付与跨职能协作",
            "requirement_kind": "must",
            "evidence_state": "partial",
            "jd_segment_refs": [jd_ref],
            "resume_segment_refs": [resume_ref],
            "limitation": "简历尚未说明交付范围。",
        }],
        "overall_advice": [{
            "action": "补充可核实的交付范围。",
            "reason": "JD 明确要求可靠交付。",
        }],
        "section_reviews": [{
            "section_name": "简历正文",
            "resume_segment_start_ref": resume_ref,
            "resume_segment_end_ref": resume_ref,
            "assessment": "aligned",
            "conclusion": "已有相关协作经历。",
            "reasoning": "正文明确展示了与研发和销售协作。",
            "preparation_points": ["准备说明本人在上线过程中的职责。"],
            "improvements": ["补充范围和结果。"],
            "rewrites": [],
        }],
        "major_gaps": [],
        "next_steps": [],
        "analysis_caveats": [],
    }


def _summarized_report() -> dict:
    report = _report()
    report["requirement_assessments"][0]["resume_segment_refs"] = []
    report["requirement_assessments"][0]["limitation"] = (
        "压缩摘要中未见完整证据，可能因摘要丢失。"
    )
    report["section_reviews"] = []
    return report


def _workflow(db_path: str, factory, *, output_locale="zh-CN") -> ResumeAdaptationWorkflow:
    return ResumeAdaptationWorkflow(
        db_path,
        model_string="deepseek:deepseek-chat",
        strict_offline=False,
        context_window=1_000_000,
        max_output_tokens=393_216,
        llm_factory=factory,
        output_locale=output_locale,
    )


def test_resume_adaptation_keeps_both_locale_artifacts(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(
        db_path,
        prep=_research_prep(attempt_state="disabled", include_snapshot=False),
    )
    zh = _workflow(db_path, lambda: CapturingLLM([_report()]))
    en = _workflow(db_path, lambda: CapturingLLM([_report()]), output_locale="en")

    for workflow in (zh, en):
        result = asyncio.run(workflow.generate(
            USER,
            application_id,
            refresh=False,
            expected_resume_id=resume_id,
            accept_no_research=True,
            accept_summarized=False,
        ))
        assert result["state"] == "ok"

    with read_connection(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?", (application_id,)
        ).fetchone()[0])
    assert prep["localized"]["zh-CN"]["resume_adaptation"]["content_locale"] == "zh-CN"
    assert prep["localized"]["en"]["resume_adaptation"]["content_locale"] == "en"
    assert zh.inspect(USER, application_id)["envelope"]["content_locale"] == "zh-CN"
    english = en.inspect(USER, application_id)
    assert english["envelope"]["content_locale"] == "en"
    assert english["host_limitations"][0].startswith("The analysis uses extracted text only")


def test_one_unbound_resume_requires_visible_binding_and_preserves_research(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    prep = _research_prep()
    prep.update({
        "resume_adaptation": {"old": True},
        "resume_adaptation_summary": {"old": True},
        "nontech_answers": [{"old": True}],
        "unknown": {"keep": True},
    })
    application_id, resume_id = _seed(db_path, bound=False, prep=prep)
    workflow = _workflow(db_path, lambda: (_ for _ in ()).throw(AssertionError("no model")))

    state = workflow.inspect(USER, application_id)
    assert state["state"] == "resume_selection_required"
    assert state["recommended_resume_id"] == resume_id

    binding = applications.bind_application_resume(
        db_path,
        USER,
        application_id,
        resume_id,
        expected_edit_revision=0,
    )
    assert binding["resume_id"] == resume_id and binding["edit_revision"] == 1
    ApplicationResumeBindingResponse.model_validate(binding)
    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT prep_status, prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
    persisted = json.loads(row[1])
    assert row[0] == "ready"
    assert persisted["research_snapshot"] == prep["research_snapshot"]
    assert persisted["research_attempt"] == prep["research_attempt"]
    assert persisted["unknown"] == {"keep": True}
    assert persisted["nontech_answers"] == [{"old": True}]
    assert not {
        "resume_adaptation", "resume_adaptation_summary",
    } & persisted.keys()


def test_adaptation_snapshot_loads_full_text_only_for_bound_resume(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO resumes "
            "(user_id, name, content_text, content_hash, extraction_receipt_json, "
            "segments_json, binding, archived, created_time, updated_time) "
            "VALUES (?, '未绑定大简历', ?, ?, '{}', '[]', 'family', 0, ?, ?)",
            (USER, "x" * 100_000, hashlib.sha256(("x" * 100_000).encode()).hexdigest(),
             NOW.isoformat(), NOW.isoformat()),
        )

    snapshot = applications.freeze_resume_adaptation_input(db_path, USER, application_id)

    assert snapshot["bound_resume"]["id"] == resume_id
    assert snapshot["bound_resume"]["content_text"] == RESUME
    assert len(snapshot["resumes"]) == 2
    assert all(set(item) == {"id", "name", "updated_time"}
               for item in snapshot["resumes"])


def test_applications_public_cas_seam_refreezes_and_key_merges(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    prep = _research_prep()
    prep["unknown"] = {"keep": True}
    application_id, resume_id = _seed(db_path, prep=prep)
    resume_hash = hashlib.sha256(RESUME.encode("utf-8")).hexdigest()
    validator_calls = 0

    def current_validator(frozen: dict, expected_input_hash: str) -> bool:
        nonlocal validator_calls
        validator_calls += 1
        assert frozen["application_id"] == application_id
        assert frozen["bound_resume"]["id"] == resume_id
        current_hash = hashlib.sha256(
            frozen["bound_resume"]["content_text"].encode("utf-8")
        ).hexdigest()
        return current_hash == expected_input_hash

    stored = applications.merge_resume_adaptation_key_if_current(
        db_path,
        USER,
        application_id,
        key="resume_adaptation_summary",
        value={
            "resume_content_hash": resume_hash,
            "summary_text": "可信摘要",
        },
        expected_input_hash=resume_hash,
        current_validator=current_validator,
    )
    stale = applications.merge_resume_adaptation_key_if_current(
        db_path,
        USER,
        application_id,
        key="resume_adaptation_summary",
        value={
            "resume_content_hash": "0" * 64,
            "summary_text": "不得覆盖",
        },
        expected_input_hash="0" * 64,
        current_validator=current_validator,
    )

    assert stored is True and stale is False and validator_calls == 2
    with read_connection(db_path) as conn:
        persisted = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
    assert persisted["resume_adaptation_summary"]["summary_text"] == "可信摘要"
    assert persisted["research_snapshot"] == prep["research_snapshot"]
    assert persisted["research_attempt"] == prep["research_attempt"]
    assert persisted["unknown"] == {"keep": True}


def test_inspect_is_zero_model_and_reports_ready(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, _resume_id = _seed(db_path)
    calls = 0

    def forbidden_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("GET must not construct a provider")

    state = _workflow(db_path, forbidden_factory).inspect(USER, application_id)

    assert state["state"] == "ready"
    assert state["estimated_input_tokens"] > 0
    assert state["model_disclosure"]["provider"] == "deepseek"
    assert calls == 0
    ResumeAdaptationResponse.model_validate(state)


def test_inspect_restores_running_generation_until_shared_task_finishes(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingLLM(CapturingLLM):
            async def chat(self, messages, *, tools=None, **kwargs):
                entered.set()
                await release.wait()
                return await super().chat(messages, tools=tools, **kwargs)

        workflow = _workflow(db_path, lambda: BlockingLLM([_report()]))
        generation = asyncio.create_task(workflow.generate(
            USER,
            application_id,
            refresh=False,
            expected_resume_id=resume_id,
            accept_no_research=False,
            accept_summarized=False,
        ))
        await asyncio.wait_for(entered.wait(), timeout=1)

        restored = workflow.inspect(USER, application_id)
        assert restored["state"] == "generation_running"
        assert restored["report"] is None
        ResumeAdaptationResponse.model_validate(restored)

        release.set()
        generated = await generation
        assert generated["state"] == "ok"
        assert workflow.inspect(USER, application_id)["state"] == "ok"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("attempt_state", "malformed_snapshot", "expected_action"),
    [
        ("idle", False, "start"),
        ("succeeded", False, "restart"),
        ("succeeded", True, "restart"),
    ],
)
def test_missing_research_snapshot_distinguishes_first_run_from_recovery(
    tmp_path,
    attempt_state,
    malformed_snapshot,
    expected_action,
):
    db_path = str(tmp_path / "careerdesk.db")
    prep = _research_prep(attempt_state=attempt_state, include_snapshot=False)
    if malformed_snapshot:
        prep["research_snapshot"] = {"snapshot_version": "corrupt"}
    application_id, _resume_id = _seed(
        db_path,
        prep=prep,
    )

    state = _workflow(db_path, lambda: None).inspect(USER, application_id)

    assert state["state"] == "research_required"
    assert state["research"]["artifact_state"] == "missing"
    assert state["research"]["attempt_state"] == attempt_state
    assert state["research"]["action"] == expected_action


def test_generate_materializes_host_evidence_persists_and_reuses_cache(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    llm = CapturingLLM([_report()])
    workflow = _workflow(db_path, lambda: llm)

    generated = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))

    assert generated["state"] == "ok" and generated["cached"] is False
    ResumeAdaptationResponse.model_validate(generated)
    evidence = generated["report"]["requirement_assessments"][0]
    assert evidence["jd_evidence"][0]["text"] == JD
    assert evidence["resume_evidence"][0]["text"] == RESUME
    assert len(llm.requests) == 1
    wire_payload = next(
        str(message["content"])
        for message in llm.requests[0]["messages"]
        if message["role"] == "user"
    )
    assert "https://example.com" not in wire_payload
    assert "application_id" not in wire_payload and "resume_id" not in wire_payload
    assert "官网与员工评论对远程政策的描述不一致" in wire_payload
    assert '"sources":["C1","C2"]' in wire_payload
    assert any("调研来源存在冲突" in item for item in generated["host_limitations"])

    cached = workflow.inspect(USER, application_id)
    assert cached["state"] == "ok" and cached["cached"] is True
    preview = workflow.input_preview(USER, application_id)
    assert preview["input_form"] == "full_text" and preview["text"] == RESUME


def test_normalized_advice_is_identical_in_generation_cache_and_http(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_RUNTIME_MODE", "test")
    monkeypatch.setenv("APP_DEV_FAKE_USER", USER)
    monkeypatch.setenv("APP_GATEWAY_AUTH_SECRET", "")
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.setenv("APP_LLM_CONTEXT_WINDOW", "")
    monkeypatch.setenv("APP_LLM_MAX_OUTPUT_TOKENS", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    raw = _report()
    raw["overall_advice"][0]["action"] = "明确写出负责可靠交付的经历。"
    workflow = _workflow(db_path, lambda: CapturingLLM([raw]))
    generated = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))
    cached = workflow.inspect(USER, application_id)

    try:
        with TestClient(create_app()) as client:
            response = client.get(
                f"/api/timeline/applications/{application_id}/resume-adaptation",
            )
    finally:
        get_settings.cache_clear()

    normalized = generated["report"]["overall_advice"][0]["action"]
    assert normalized.startswith("若你确实有可核实的相关经历，")
    assert cached["report"]["overall_advice"][0]["action"] == normalized
    assert response.status_code == 200, response.text
    assert response.json()["report"]["overall_advice"][0]["action"] == normalized


@pytest.mark.parametrize("tamper", ["extra", "offset", "reference"])
def test_tampered_materialized_cache_is_a_clean_cache_miss(tmp_path, tamper):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    workflow = _workflow(db_path, lambda: CapturingLLM([_report()]))
    generated = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))
    assert generated["state"] == "ok"

    with transaction(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
        report = prep["localized"]["zh-CN"]["resume_adaptation"]["report"]
        if tamper == "extra":
            report["content_text"] = "host-only secret must not escape"
        elif tamper == "offset":
            report["requirement_assessments"][0]["jd_evidence"][0]["char_end"] += 1
        else:
            report["requirement_assessments"][0]["jd_segment_refs"] = [
                "J1-9999-deadbeef",
            ]
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            (json.dumps(prep, ensure_ascii=False), application_id),
        )

    cache_miss = _workflow(
        db_path,
        lambda: (_ for _ in ()).throw(AssertionError("GET must not build a model")),
    ).inspect(USER, application_id)

    assert cache_miss["state"] == "ready"
    assert cache_miss["report"] is None
    assert cache_miss["envelope"] is None
    assert "host-only secret" not in json.dumps(cache_miss, ensure_ascii=False)


def test_http_tampered_adaptation_cache_returns_clean_state_instead_of_500(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_RUNTIME_MODE", "test")
    monkeypatch.setenv("APP_DEV_FAKE_USER", USER)
    monkeypatch.setenv("APP_GATEWAY_AUTH_SECRET", "")
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.setenv("APP_LLM_CONTEXT_WINDOW", "")
    monkeypatch.setenv("APP_LLM_MAX_OUTPUT_TOKENS", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    generated = asyncio.run(_workflow(
        db_path,
        lambda: CapturingLLM([_report()]),
    ).generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))
    assert generated["state"] == "ok"
    with transaction(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
        prep["localized"]["zh-CN"]["resume_adaptation"]["report"]["file_path"] = "/private/host/path"
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            (json.dumps(prep, ensure_ascii=False), application_id),
        )

    try:
        with TestClient(create_app()) as client:
            response = client.get(
                f"/api/timeline/applications/{application_id}/resume-adaptation",
            )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "model_required"
    assert response.json()["report"] is None
    assert "/private/host/path" not in response.text


def test_invalid_host_reference_rejects_whole_report(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    llm = CapturingLLM([
        _report(bad_ref=True),
        _report(bad_ref=True),
        _report(bad_ref=True),
    ])
    workflow = _workflow(db_path, lambda: llm)

    result = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))

    assert result["state"] == "invalid_model_output"
    assert len(llm.requests) == 3
    with read_connection(db_path) as conn:
        prep_json = conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?", (application_id,),
        ).fetchone()[0]
    assert "resume_adaptation" not in json.loads(prep_json)


def test_host_validation_failure_gets_one_fresh_retry_and_can_succeed(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    canary = "HOST_INVALID_OUTPUT_MUST_NOT_ENTER_RETRY_CONTEXT"
    invalid = _report(bad_ref=True)
    invalid["summary_sentences"] = [f"{canary}。"]
    llm = CapturingLLM([invalid, _report()])
    workflow = _workflow(db_path, lambda: llm)

    result = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))

    assert result["state"] == "ok"
    assert len(llm.requests) == 2
    retry_request = json.dumps(llm.requests[1]["messages"], ensure_ascii=False)
    assert canary not in retry_request
    assert "校验失败：" in retry_request


def test_unsupported_optional_rewrite_is_dropped_without_retry_or_persistence(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    invalid = _report()
    resume_ref = exact_text_segments(RESUME, namespace="R")[0].segment_id
    invalid["section_reviews"][0]["rewrites"] = [{
        "resume_segment_ref": resume_ref,
        "suggestion": "前置 AWS 基础设施交付。",
        "reason": "提升可扫描性。",
        "verification_needed": False,
    }]
    llm = CapturingLLM([invalid])
    workflow = _workflow(db_path, lambda: llm)

    result = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))

    assert result["state"] == "ok"
    assert result["report"]["section_reviews"][0]["rewrites"] == []
    assert len(llm.requests) == 1


def test_no_research_narrow_gate_requires_per_call_confirmation(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(
        db_path,
        prep=_research_prep(attempt_state="disabled", include_snapshot=False),
    )
    llm = CapturingLLM([_report()])
    workflow = _workflow(db_path, lambda: llm)

    blocked = workflow.inspect(USER, application_id)
    assert blocked["state"] == "research_disabled"
    assert blocked["no_research_fallback_available"] is True
    still_blocked = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))
    assert still_blocked["state"] == "research_disabled" and not llm.requests

    generated = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=True,
        accept_summarized=False,
    ))
    assert generated["state"] == "ok"
    assert generated["envelope"]["research_mode"] == "no_research"
    assert any("未结合公司调研" in item for item in generated["host_limitations"])
    wire_payload = next(
        str(message["content"])
        for message in llm.requests[0]["messages"]
        if message["role"] == "user"
    )
    assert '"research"' not in wire_payload


def test_generate_treats_expected_resume_id_as_optional_precondition(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, _resume_id = _seed(db_path)
    llm = CapturingLLM([_report()])

    result = asyncio.run(_workflow(db_path, lambda: llm).generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=None,
        accept_no_research=False,
        accept_summarized=False,
    ))

    assert result["state"] == "ok"
    assert len(llm.requests) == 1


def test_cached_generate_still_enforces_expected_resume_precondition(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)
    llm = CapturingLLM([_report()])
    workflow = _workflow(db_path, lambda: llm)

    generated = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))
    stale = asyncio.run(workflow.generate(
        USER,
        application_id,
        refresh=False,
        expected_resume_id=resume_id + 1,
        accept_no_research=False,
        accept_summarized=False,
    ))

    assert generated["state"] == "ok"
    assert stale["state"] == "stale"
    assert len(llm.requests) == 1


def test_research_refresh_claim_during_generation_rejects_old_snapshot_publish(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)

    async def race() -> dict:
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingLLM(CapturingLLM):
            async def chat(self, messages, *, tools=None, **kwargs):
                entered.set()
                await release.wait()
                return await super().chat(messages, tools=tools, **kwargs)

        workflow = _workflow(db_path, lambda: BlockingLLM([_report()]))
        task = asyncio.create_task(workflow.generate(
            USER,
            application_id,
            refresh=True,
            expected_resume_id=resume_id,
            accept_no_research=False,
            accept_summarized=False,
        ))
        await entered.wait()
        with transaction(db_path) as conn:
            prep = json.loads(conn.execute(
                "SELECT prep_json FROM applications WHERE id = ?",
                (application_id,),
            ).fetchone()[0])
            prep["research_attempt"] = {
                "attempt_state": "running",
                "generation": "refresh-owner",
                "updated_time": NOW.isoformat(),
                "error_code": None,
            }
            conn.execute(
                "UPDATE applications SET prep_json = ? WHERE id = ?",
                (json.dumps(prep, ensure_ascii=False), application_id),
            )
        release.set()
        return await task

    result = asyncio.run(race())

    assert result["state"] == "stale"
    with read_connection(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
    assert "resume_adaptation" not in prep
    assert prep["research_attempt"]["attempt_state"] == "running"


def test_refresh_requests_share_one_inflight_model_task(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)

    async def race() -> tuple[int, int, list[dict]]:
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingLLM(CapturingLLM):
            async def chat(self, messages, *, tools=None, **kwargs):
                entered.set()
                await release.wait()
                return await super().chat(messages, tools=tools, **kwargs)

        llm = BlockingLLM([_report()])
        factory_calls = 0

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            return llm

        first = asyncio.create_task(_workflow(db_path, factory).generate(
            USER,
            application_id,
            refresh=True,
            expected_resume_id=resume_id,
            accept_no_research=False,
            accept_summarized=False,
        ))
        await entered.wait()
        second = asyncio.create_task(_workflow(db_path, factory).generate(
            USER,
            application_id,
            refresh=True,
            expected_resume_id=resume_id,
            accept_no_research=False,
            accept_summarized=False,
        ))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        return factory_calls, len(llm.requests), results

    factory_calls, request_count, results = asyncio.run(race())

    assert factory_calls == 1
    assert request_count == 1
    assert [item["state"] for item in results] == ["ok", "ok"]
    assert results[0]["report"] == results[1]["report"]


def test_summary_path_has_shared_hard_timeout_and_persists_no_partial_cache(
    tmp_path,
    monkeypatch,
):
    from careerdesk.orchestration.application_prep import adaptation_workflow as module

    db_path = str(tmp_path / "careerdesk.db")
    application_id, _resume_id = _seed(db_path)
    workflow = _workflow(db_path, lambda: CapturingLLM([]))
    snapshot = workflow._read_snapshot(USER, application_id)
    expected_full_input_hash = workflow._input_for_form(
        snapshot,
        research_mode="snapshot",
        resume_input_form="full_text",
    ).input_hash

    async def hung_summary(_llm, _snapshot, _target_chars):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "ADAPTATION_JOB_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(workflow, "_build_summary", hung_summary)
    result = asyncio.run(workflow._run_with_hard_timeout(
        USER,
        application_id,
        snapshot,
        research_mode="snapshot",
        use_summary=True,
        target_chars=200,
        expected_full_input_hash=expected_full_input_hash,
    ))

    assert result["state"] == "provider_error"
    with read_connection(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
    assert "resume_adaptation_summary" not in prep
    assert "resume_adaptation" not in prep


def test_persisted_summary_is_reused_after_an_adaptation_failure(tmp_path, monkeypatch):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, _resume_id = _seed(db_path)
    llm = CapturingLLM([_summarized_report(), _summarized_report()])
    workflow = _workflow(db_path, lambda: llm)
    summary_calls = 0

    async def build_summary(_llm, snapshot, target_chars):
        nonlocal summary_calls
        summary_calls += 1
        text = "产品经理；企业产品路线图；跨职能协作交付。"[:target_chars]
        return {
            "resume_content_hash": hashlib.sha256(
                snapshot["bound_resume"]["content_text"].encode("utf-8")
            ).hexdigest(),
            "summary_policy_version": 1,
            "target_chars": target_chars,
            "summary_text": text,
            "summary_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chunk_count": 1,
            "generated_time": NOW.isoformat(),
        }

    monkeypatch.setattr(workflow, "_build_summary", build_summary)
    first_snapshot = workflow._read_snapshot(USER, application_id)
    first = asyncio.run(workflow._run_shared(
        USER,
        application_id,
        first_snapshot,
        research_mode="snapshot",
        use_summary=True,
        target_chars=200,
        expected_full_input_hash=workflow._input_for_form(
            first_snapshot,
            research_mode="snapshot",
            resume_input_form="full_text",
        ).input_hash,
    ))
    # Simulate a retryable adaptation loss while keeping the separately paid
    # summary cache.  The next refresh must not call the summarizer again.
    with transaction(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
        prep.pop("resume_adaptation", None)
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            (json.dumps(prep, ensure_ascii=False), application_id),
        )
    second_snapshot = workflow._read_snapshot(USER, application_id)
    second = asyncio.run(workflow._run_shared(
        USER,
        application_id,
        second_snapshot,
        research_mode="snapshot",
        use_summary=True,
        target_chars=200,
        expected_full_input_hash=workflow._input_for_form(
            second_snapshot,
            research_mode="snapshot",
            resume_input_form="full_text",
        ).input_hash,
    ))

    assert first["state"] == second["state"] == "ok"
    assert summary_calls == 1
    assert len(llm.requests) == 2


@pytest.mark.parametrize("concurrent_change", ["jd", "same_content_binding"])
def test_summary_task_does_not_follow_a_concurrently_changed_adaptation_input(
    tmp_path,
    monkeypatch,
    concurrent_change,
):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, original_resume_id = _seed(db_path)
    llm = CapturingLLM([])
    workflow = _workflow(db_path, lambda: llm)
    original_snapshot = workflow._read_snapshot(USER, application_id)
    expected_full_input_hash = workflow._input_for_form(
        original_snapshot,
        research_mode="snapshot",
        resume_input_form="full_text",
    ).input_hash
    replacement_resume_id = None

    async def build_summary(_llm, snapshot, target_chars):
        nonlocal replacement_resume_id
        with transaction(db_path) as conn:
            if concurrent_change == "jd":
                conn.execute(
                    "UPDATE applications SET jd_text = ? WHERE id = ?",
                    ("更新后的岗位要求：负责增长实验。", application_id),
                )
            else:
                replacement_resume_id = conn.execute(
                    "INSERT INTO resumes "
                    "(user_id, name, content_text, content_hash, extraction_receipt_json, "
                    "segments_json, binding, archived, created_time, updated_time) "
                    "VALUES (?, '相同内容的新版本', ?, ?, '{}', '[]', 'family', 0, ?, ?)",
                    (USER, RESUME, hashlib.sha256(RESUME.encode()).hexdigest(),
                     NOW.isoformat(), NOW.isoformat()),
                ).lastrowid
                conn.execute(
                    "UPDATE applications SET resume_id = ? WHERE id = ?",
                    (replacement_resume_id, application_id),
                )
        text = "产品经理；企业产品路线图；跨职能协作交付。"[:target_chars]
        return {
            "resume_content_hash": hashlib.sha256(
                snapshot["bound_resume"]["content_text"].encode("utf-8")
            ).hexdigest(),
            "summary_policy_version": 1,
            "target_chars": target_chars,
            "summary_text": text,
            "summary_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chunk_count": 1,
            "generated_time": NOW.isoformat(),
        }

    monkeypatch.setattr(workflow, "_build_summary", build_summary)
    result = asyncio.run(workflow._run_shared(
        USER,
        application_id,
        original_snapshot,
        research_mode="snapshot",
        use_summary=True,
        target_chars=200,
        expected_full_input_hash=expected_full_input_hash,
    ))

    assert result["state"] == "stale"
    assert llm.requests == []
    if concurrent_change == "same_content_binding":
        assert replacement_resume_id is not None
        assert replacement_resume_id != original_resume_id
        assert result["bound_resume"]["id"] == replacement_resume_id
    with read_connection(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
    assert "resume_adaptation_summary" in prep
    assert "resume_adaptation" not in prep


def test_failed_refresh_with_a_new_summary_target_keeps_old_report_readable(
    tmp_path,
    monkeypatch,
):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, _resume_id = _seed(db_path)
    first_llm = CapturingLLM([_summarized_report()])
    first_workflow = _workflow(db_path, lambda: first_llm)

    async def build_summary(_llm, snapshot, target_chars):
        text = (f"目标{target_chars}；产品路线图；跨职能协作交付。")[:target_chars]
        return {
            "resume_content_hash": hashlib.sha256(
                snapshot["bound_resume"]["content_text"].encode("utf-8")
            ).hexdigest(),
            "summary_policy_version": 1,
            "target_chars": target_chars,
            "summary_text": text,
            "summary_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chunk_count": 1,
            "generated_time": NOW.isoformat(),
        }

    monkeypatch.setattr(first_workflow, "_build_summary", build_summary)
    first_snapshot = first_workflow._read_snapshot(USER, application_id)
    first = asyncio.run(first_workflow._run_shared(
        USER,
        application_id,
        first_snapshot,
        research_mode="snapshot",
        use_summary=True,
        target_chars=200,
        expected_full_input_hash=first_workflow._input_for_form(
            first_snapshot,
            research_mode="snapshot",
            resume_input_form="full_text",
        ).input_hash,
    ))
    assert first["state"] == "ok"

    class FailingRefreshLLM(CapturingLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            self.requests.append({"messages": messages, "tools": tools, "kwargs": kwargs})
            raise RuntimeError("synthetic provider failure")

    refresh_llm = FailingRefreshLLM([])
    refresh_workflow = ResumeAdaptationWorkflow(
        db_path,
        model_string="deepseek:deepseek-reasoner",
        strict_offline=False,
        context_window=500_000,
        max_output_tokens=65_536,
        llm_factory=lambda: refresh_llm,
    )
    monkeypatch.setattr(refresh_workflow, "_build_summary", build_summary)
    refresh_snapshot = refresh_workflow._read_snapshot(USER, application_id)
    failed_refresh = asyncio.run(refresh_workflow._run_shared(
        USER,
        application_id,
        refresh_snapshot,
        research_mode="snapshot",
        use_summary=True,
        target_chars=120,
        expected_full_input_hash=refresh_workflow._input_for_form(
            refresh_snapshot,
            research_mode="snapshot",
            resume_input_form="full_text",
        ).input_hash,
    ))
    cached = refresh_workflow.inspect(USER, application_id)

    assert failed_refresh["state"] == "provider_error"
    assert len(refresh_llm.requests) == 1
    assert cached["state"] == "ok"
    assert cached["cached"] is True
    assert cached["report"] == first["report"]
    assert cached["envelope"] == first["envelope"]
    with read_connection(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
    summary_cache = prep["resume_adaptation_summary"]
    assert summary_cache["cache_version"] == 1
    assert [entry["target_chars"] for entry in summary_cache["entries"]] == [120, 200]


def test_summary_cache_migrates_legacy_entry_is_bounded_and_fails_closed(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, _resume_id = _seed(db_path)
    workflow = _workflow(db_path, lambda: CapturingLLM([]))
    resume_hash = hashlib.sha256(RESUME.encode("utf-8")).hexdigest()

    def entry(target_chars):
        text = f"目标{target_chars}的事实摘要。"
        return {
            "resume_content_hash": resume_hash,
            "summary_policy_version": 1,
            "target_chars": target_chars,
            "summary_text": text,
            "summary_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chunk_count": 1,
            "generated_time": NOW.isoformat(),
        }

    legacy = entry(100)
    with transaction(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
        prep["resume_adaptation_summary"] = legacy
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            (json.dumps(prep, ensure_ascii=False), application_id),
        )
    assert workflow._valid_summary_cache(
        workflow._read_snapshot(USER, application_id),
        target_chars=100,
    ) == legacy

    for target in (200, 300, 400, 500, 600):
        assert workflow._persist_summary_if_current(
            USER,
            application_id,
            entry(target),
        ) is not None
    snapshot = workflow._read_snapshot(USER, application_id)
    container = snapshot["prep"]["resume_adaptation_summary"]
    assert set(container) == {"cache_version", "resume_content_hash", "entries"}
    assert len(container["entries"]) == 4
    assert [item["target_chars"] for item in container["entries"]] == [600, 500, 400, 300]

    with transaction(db_path) as conn:
        prep = json.loads(conn.execute(
            "SELECT prep_json FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()[0])
        prep["resume_adaptation_summary"]["entries"][0]["unknown"] = True
        conn.execute(
            "UPDATE applications SET prep_json = ? WHERE id = ?",
            (json.dumps(prep, ensure_ascii=False), application_id),
        )
    assert workflow._valid_summary_cache_entries(
        workflow._read_snapshot(USER, application_id),
    ) == []


def test_provider_error_never_reflects_resume_or_sdk_exception_text(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    application_id, resume_id = _seed(db_path)

    class LeakyProviderLLM(CapturingLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            raise RuntimeError(f"provider leaked request body: {RESUME}")

    result = asyncio.run(_workflow(
        db_path,
        lambda: LeakyProviderLLM([]),
    ).generate(
        USER,
        application_id,
        refresh=True,
        expected_resume_id=resume_id,
        accept_no_research=False,
        accept_summarized=False,
    ))

    rendered = json.dumps(result, ensure_ascii=False)
    assert result["state"] == "provider_error"
    assert "provider leaked" not in rendered
    assert RESUME not in rendered


def test_malformed_persisted_research_error_is_not_reflected(tmp_path):
    db_path = str(tmp_path / "careerdesk.db")
    prep = _research_prep()
    prep["research_attempt"] = {
        "attempt_state": "failed",
        "generation": None,
        "updated_time": "not-an-iso-time",
        "error_code": "provider raw body: host-only secret",
    }
    application_id, _resume_id = _seed(db_path, prep=prep)

    result = _workflow(db_path, lambda: object()).inspect(USER, application_id)

    assert result["research"]["attempt_state"] == "idle"
    assert result["research"]["error_code"] is None
    assert "host-only secret" not in json.dumps(result, ensure_ascii=False)


def test_adaptation_read_routes_are_pure_and_binding_requires_explicit_resume_id(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_RUNTIME_MODE", "test")
    monkeypatch.setenv("APP_DEV_FAKE_USER", USER)
    monkeypatch.setenv("APP_GATEWAY_AUTH_SECRET", "")
    monkeypatch.setenv("APP_LLM_MODEL", "")
    monkeypatch.setenv("APP_LLM_CONTEXT_WINDOW", "")
    monkeypatch.setenv("APP_LLM_MAX_OUTPUT_TOKENS", "")
    get_settings.cache_clear()
    from careerdesk.bootstrap.app import create_app

    try:
        with TestClient(create_app()) as client:
            db_path = str(tmp_path / "careerdesk.db")
            application_id, resume_id = _seed(db_path)
            with read_connection(db_path) as conn:
                before = conn.execute(
                    "SELECT resume_id, prep_json, updated_time FROM applications WHERE id = ?",
                    (application_id,),
                ).fetchone()

            state = client.get(
                f"/api/timeline/applications/{application_id}/resume-adaptation",
            )
            preview = client.get(
                f"/api/timeline/applications/{application_id}/resume-adaptation/input-preview",
            )
            omitted = client.put(
                f"/api/timeline/applications/{application_id}/resume-binding",
                json={"expected_edit_revision": 0},
            )
            explicit_unbind = client.put(
                f"/api/timeline/applications/{application_id + 10_000}/resume-binding",
                json={"resume_id": None, "expected_edit_revision": 0},
            )
            with read_connection(db_path) as conn:
                after = conn.execute(
                    "SELECT resume_id, prep_json, updated_time FROM applications WHERE id = ?",
                    (application_id,),
                ).fetchone()
    finally:
        get_settings.cache_clear()

    assert state.status_code == 200, state.text
    assert state.json()["state"] == "model_required"
    assert state.json()["bound_resume"]["id"] == resume_id
    assert preview.status_code == 200
    assert preview.json()["input_form"] == "full_text"
    assert preview.json()["text"] == RESUME
    assert before == after
    assert omitted.status_code == 422
    # Explicit null passes body validation and reaches the missing application.
    assert explicit_unbind.status_code == 404


def test_http_response_models_reject_unknown_top_level_fields():
    application_response = ApplicationResumeBindingResponse.model_validate({
        "resume_id": None,
        "edit_revision": 0,
        "bound_resume": None,
    })
    assert application_response.resume_id is None

    payload = {
        "state": "no_resume",
        "message": None,
        "cached": False,
        "bound_resume": None,
        "resume_options": [],
        "recommended_resume_id": None,
        "research": None,
        "report": None,
        "envelope": None,
        "host_limitations": [],
        "analysis_flags": [],
        "estimated_input_tokens": None,
        "model_disclosure": None,
        "summarization_available": False,
        "no_research_fallback_available": False,
        "model_input_preview_available": False,
    }
    assert ResumeAdaptationResponse.model_validate(payload).state == "no_resume"
    with pytest.raises(ValidationError):
        ResumeAdaptationResponse.model_validate({**payload, "internal": "must not escape"})


def _strict_nested_response_payload() -> dict:
    materialized_report = validate_and_materialize_report(
        ResumeAdaptationReport.model_validate(_report()),
        jd_segments=exact_text_segments(JD, namespace="J"),
        resume_segments=exact_text_segments(RESUME, namespace="R"),
        resume_input_form="full_text",
    )
    resume = {
        "id": 7,
        "name": "产品岗位版",
        "updated_time": NOW.isoformat(),
        "extraction_receipt": {
            "status": "usable",
            "char_count": 42,
            "non_whitespace_count": 38,
            "alnum_count": 30,
            "replacement_char_count": 0,
            "replacement_ratio": 0.0,
            "control_char_count": 0,
            "control_ratio": 0.0,
            "reason_codes": [],
            "warning_codes": [],
        },
    }
    return {
        "state": "ok",
        "message": None,
        "cached": False,
        "bound_resume": resume,
        "resume_options": [{**resume, "extraction_receipt": None}],
        "recommended_resume_id": None,
        "research": {
            "artifact_state": "ready",
            "attempt_state": "succeeded",
            "coverage_quality": "complete",
            "fresh_until": NOW.isoformat(),
            "error_code": None,
            "action": None,
        },
        "report": materialized_report,
        "envelope": {
            "artifact_version": 1,
            "resume_id": 7,
            "resume_name": "产品岗位版",
            "resume_selection": "bound",
            "research_mode": "snapshot",
            "research_snapshot_id": "0123456789abcdef0123456789abcdef",
            "resume_input_form": "full_text",
            "generated_time": NOW.isoformat(),
            "content_locale": "zh-CN",
        },
        "host_limitations": [],
        "analysis_flags": [],
        "estimated_input_tokens": 1,
        "model_disclosure": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "label": "deepseek · deepseek-chat",
        },
        "summarization_available": False,
        "no_research_fallback_available": False,
        "model_input_preview_available": True,
    }


@pytest.mark.parametrize(
    ("target", "extra_key"),
    [
        ("bound_resume", "content_text"),
        ("resume_option", "url"),
        ("extraction_receipt", "file_path"),
        ("research", "semantic_claim_hash"),
        ("model_disclosure", "endpoint"),
        ("envelope", "input_hash"),
        ("report", "input_hash"),
        ("requirement", "file_path"),
        ("evidence", "url"),
        ("segment_range", "content_text"),
    ],
)
def test_resume_adaptation_nested_http_contracts_reject_host_only_extras(
    target,
    extra_key,
):
    payload = _strict_nested_response_payload()
    if target == "resume_option":
        nested = payload["resume_options"][0]
    elif target == "extraction_receipt":
        nested = payload["bound_resume"]["extraction_receipt"]
    elif target == "requirement":
        nested = payload["report"]["requirement_assessments"][0]
    elif target == "evidence":
        nested = payload["report"]["requirement_assessments"][0]["jd_evidence"][0]
    elif target == "segment_range":
        nested = payload["report"]["section_reviews"][0]["resume_segment_range"]
    else:
        nested = payload[target]
    nested[extra_key] = "must not escape"

    with pytest.raises(ValidationError):
        ResumeAdaptationResponse.model_validate(payload)


def test_resume_adaptation_nested_http_contracts_match_public_shape():
    payload = _strict_nested_response_payload()

    rendered = ResumeAdaptationResponse.model_validate(payload).model_dump(mode="json")

    assert rendered["bound_resume"] == payload["bound_resume"]
    assert rendered["resume_options"] == payload["resume_options"]
    assert rendered["research"] == payload["research"]
    assert rendered["report"] == payload["report"]
    assert rendered["envelope"] == payload["envelope"]
    assert rendered["model_disclosure"] == payload["model_disclosure"]
