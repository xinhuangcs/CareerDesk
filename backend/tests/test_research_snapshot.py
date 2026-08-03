
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from careerdesk.features.applications import public as applications
from careerdesk.features.applications.contracts import ApplicationPrepArtifact
from careerdesk.features.research.public import (
    build_research_snapshot,
    company_cache_eligibility_hash,
    derive_research_artifact_state,
    research_semantic_claim,
    research_is_fresh,
)
from careerdesk.orchestration.application_prep.briefing import compose_briefing
from careerdesk.orchestration.application_prep.http_contracts import BriefingResponse
from careerdesk.orchestration.application_prep.service import PrepService
from careerdesk.platform.database import init_db, read_connection, transaction


UTC = timezone.utc
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _reports(*, complete: bool = True) -> tuple[dict, dict]:
    company_sources = [
        {
            "index": 1,
            "url": "https://example.com/company",
            "site": "example.com",
            "title": "Company profile",
            "date": "2026-07-10",
            "engines": ["test"],
        },
        {
            "index": 2,
            "url": "https://reviews.example/company",
            "site": "reviews.example",
            "title": "Employee review",
            "date": "2026-07-09",
            "engines": ["test"],
        },
    ]
    position_sources = [
        {
            "index": 1,
            "url": "https://example.com/job",
            "site": "example.com",
            "title": "Job profile",
            "date": "2026-07-11",
            "engines": ["test"],
        }
    ]
    company = {
        "version": 3,
        "anchor": {"official_name": "示例公司"},
        "business": {"text": "主营企业软件", "sources": [1]},
        "culture": {"text": "重视工程质量", "sources": [1]},
        "recent_news": {"text": "发布新产品", "sources": [1]},
        "interview_style": {"text": "重视系统设计", "sources": [1]},
        "source_conflicts": [
            {
                "summary": "官网与员工评论对远程政策的描述不一致。",
                "sources": [1, 2],
            }
        ],
        "sources": company_sources,
        "planner": {"debug": "must not enter the snapshot"},
    }
    position = {
        "key_takeaways": ["Python"],
        "interview_process": {"text": "两轮技术面", "sources": [1]},
        "experience_highlights": {"text": "分布式系统", "sources": [1]},
        "team_and_work_context": {"text": "Python 服务", "sources": [1]},
        "reported_questions": [{
            "text": "如何限流？", "category": "professional_domain",
            "provenance": "reported", "sources": [1],
        }],
        "likely_questions": [],
        "assessment_focuses": [],
        "source_conflicts": [],
        "sources": position_sources,
    }
    if not complete:
        company["culture"] = {"text": "未找到可靠公开信息", "sources": []}
    return company, position


def _semantic_claim(*, position: str = "后端工程师") -> dict:
    return research_semantic_claim(
        company="示例公司",
        aliases=["Example Co"],
        notes="企业软件公司",
        department="平台研发",
        position=position,
        jd_text="负责 Python 服务",
    )


def _snapshot(*, complete: bool = True, position: str = "后端工程师") -> dict:
    company, position_report = _reports(complete=complete)
    return build_research_snapshot(
        company_report=company,
        position_report=position_report,
        semantic_claim=_semantic_claim(position=position),
        company_report_generated_time=NOW - timedelta(days=13),
        position_report_generated_time=NOW,
        snapshot_id="0123456789abcdef0123456789abcdef",
    )


def test_snapshot_is_deterministic_strict_and_namespaces_sources():
    snapshot = _snapshot()

    assert snapshot["snapshot_id"] == "0123456789abcdef0123456789abcdef"
    assert snapshot["coverage_quality"] == "complete"
    assert snapshot["company_sources"][0]["source_id"] == "C1"
    assert snapshot["position_sources"][0]["source_id"] == "P1"
    assert snapshot["company_report"]["business"]["sources"] == ["C1"]
    assert snapshot["position_report"]["reported_questions"][0]["sources"] == ["P1"]
    assert snapshot["source_conflicts"] == [
        {
            "summary": "官网与员工评论对远程政策的描述不一致。",
            "sources": ["C1", "C2"],
        }
    ]
    assert snapshot["company_report"]["source_conflicts"] == snapshot["source_conflicts"]
    assert "sources" not in snapshot["company_report"]
    assert "planner" not in snapshot["company_report"]
    assert snapshot["fresh_until"] == "2026-07-20T12:00:00+00:00"
    assert len(snapshot["company_report_hash"]) == 64
    assert len(snapshot["position_report_hash"]) == 64

    partial = _snapshot(complete=False)
    assert partial["coverage_quality"] == "partial"
    assert partial["missing_sections"] == ["company.culture"]


def test_briefing_contract_accepts_namespaced_snapshot_sources(tmp_path):
    db_path = str(tmp_path / "briefing.db")
    init_db(db_path)
    snapshot = _snapshot()
    with transaction(db_path) as conn:
        application_id = conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, department, jd_text, stage, prep_status, prep_json, "
            "created_time, updated_time) VALUES "
            "('u1', '示例公司', '后端工程师', '平台研发', '负责 Python 服务', "
            "'applied', 'ready', ?, ?, ?)",
            (json.dumps({"research_snapshot": snapshot}, ensure_ascii=False),
             NOW.isoformat(), NOW.isoformat()),
        ).lastrowid

    result = compose_briefing(db_path, "u1", application_id, today="2026-07-19")
    response = BriefingResponse.model_validate(result)

    assert response.status == "ok"
    assert response.data.research is not None
    assert response.data.research.business.sources == ["C1"]


def test_artifact_state_detects_input_hash_time_and_structural_drift():
    snapshot = _snapshot()
    current_hash = snapshot["semantic_claim_hash"]

    ready = derive_research_artifact_state(
        {"research_snapshot": snapshot},
        current_semantic_claim_hash=current_hash,
        now=NOW,
    )
    assert ready["artifact_state"] == "ready"
    assert ready["coverage_quality"] == "complete"
    assert ready["snapshot"] == snapshot

    changed = derive_research_artifact_state(
        {"research_snapshot": snapshot},
        current_semantic_claim_hash="f" * 64,
        now=NOW,
    )
    assert changed["artifact_state"] == "stale"

    future = dict(snapshot)
    future["position_report_generated_time"] = "2026-07-20T12:00:00+00:00"
    assert derive_research_artifact_state(
        {"research_snapshot": future},
        current_semantic_claim_hash=current_hash,
        now=NOW,
    )["artifact_state"] == "stale"

    corrupt = dict(snapshot)
    corrupt["company_report_hash"] = "0" * 64
    assert derive_research_artifact_state(
        {"research_snapshot": corrupt},
        current_semantic_claim_hash=current_hash,
        now=NOW,
    )["artifact_state"] == "stale"

    malformed_legacy = derive_research_artifact_state(
        {"research_snapshot": {"broken": True}, "position_report": {"old": True}},
        current_semantic_claim_hash=current_hash,
        now=NOW,
    )
    assert malformed_legacy["artifact_state"] == "legacy"
    assert malformed_legacy["snapshot"] is None
    assert derive_research_artifact_state(
        {}, current_semantic_claim_hash=current_hash, now=NOW
    )["artifact_state"] == "missing"


def test_company_cache_eligibility_hash_is_canonical_and_sensitive():
    first = company_cache_eligibility_hash(
        company="示例公司", aliases=["Example", "Demo"], notes="note"
    )
    same = company_cache_eligibility_hash(
        company="示例公司", aliases=["Example", "Demo"], notes="note"
    )
    changed = company_cache_eligibility_hash(
        company="示例公司", aliases=["Example", "Demo"], notes="changed"
    )
    assert first == same
    assert len(first) == 64
    assert changed != first
    assert not research_is_fresh("2026-07-20T12:00:00+00:00", "2026-07-19")


def test_timeline_prep_projection_excludes_large_research_and_adaptation_bodies():
    snapshot = _snapshot()
    projected = ApplicationPrepArtifact.model_validate(
        {
            "research": "ok",
            "research_snapshot": snapshot,
            "position_report": {"huge": "x" * 100_000},
            "unrelated_artifact": {"huge": "y" * 100_000},
            "resume_adaptation": {
                "artifact_version": 1,
                "input_hash": "a" * 64,
                "report": {"huge": "z" * 100_000},
            },
        }
    ).model_dump(mode="json")

    assert "position_report" not in projected
    assert "unrelated_artifact" not in projected
    assert "company_report" not in projected["research_snapshot"]
    assert "position_report" not in projected["research_snapshot"]
    assert "report" not in projected["resume_adaptation"]


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    with transaction(path) as conn:
        conn.execute(
            "INSERT INTO companies "
            "(user_id, name, aliases_json, notes, created_time, updated_time) "
            "VALUES ('u1', '示例公司', '[\"Example Co\"]', '企业软件公司', ?, ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO applications "
            "(user_id, company, position, department, jd_text, prep_status, "
            "prep_generation, prep_heartbeat_time, created_time, updated_time) "
            "VALUES ('u1', '示例公司', '后端工程师', '平台研发', '负责 Python 服务', "
            "'running', 'generation-1', ?, ?, ?)",
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
    return path


def test_application_snapshot_publish_is_generation_and_input_guarded(db_path):
    snapshot = _snapshot()
    claim = _semantic_claim()

    assert applications.merge_prep_artifacts(
        db_path,
        "u1",
        1,
        {"unknown_existing": {"keep": True}},
        generation="generation-1",
    )
    assert applications.set_research_attempt(
        db_path,
        "u1",
        1,
        {
            "attempt_state": "running",
            "generation": "generation-1",
            "updated_time": NOW.isoformat(),
            "error_code": None,
        },
        generation="generation-1",
    )
    assert applications.publish_research_snapshot(
        db_path,
        "u1",
        1,
        snapshot,
        generation="generation-1",
        expected_semantic_claim=claim,
    )

    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT prep_status, prep_generation, prep_json FROM applications WHERE id = 1"
        ).fetchone()
    import json

    prep = json.loads(row[2])
    assert row[:2] == ("running", "generation-1")
    assert prep["unknown_existing"] == {"keep": True}
    assert prep["localized"]["zh-CN"]["research_snapshot"] == snapshot
    assert prep["research_attempt"]["attempt_state"] == "succeeded"

    assert not applications.publish_research_snapshot(
        db_path,
        "u1",
        1,
        snapshot,
        generation="stale-generation",
        expected_semantic_claim=claim,
    )

    changed_claim = _semantic_claim(position="平台工程师")
    assert not applications.publish_research_snapshot(
        db_path,
        "u1",
        1,
        snapshot,
        generation="generation-1",
        expected_semantic_claim=changed_claim,
    )


def test_owned_stale_research_closes_attempt_instead_of_leaving_orphaned_lease(db_path):

    class StaleResearch:
        async def research(self, *_args, **_kwargs):
            return {
                "status": "stale",
                "company_report": None,
                "position_report": None,
            }

    result = asyncio.run(
        PrepService(db_path, StaleResearch()).run(
            "u1",
            1,
            generation="generation-1",
        )
    )

    assert result["status"] == "stale"
    with read_connection(db_path) as conn:
        prep_status, generation, raw_prep = conn.execute(
            "SELECT prep_status, prep_generation, prep_json "
            "FROM applications WHERE user_id = 'u1' AND id = 1"
        ).fetchone()
    import json

    prep = json.loads(raw_prep)
    assert prep_status == "failed"
    assert generation is None
    assert prep["research_attempt"]["attempt_state"] == "failed"
    assert prep["research_attempt"]["error_code"] == "prep_failed"
    assert "输入已更新" in prep["error"]


def test_prep_publishes_snapshot_without_retired_web_question_side_effect(db_path):
    company_report, position_report = _reports()

    class CompletedResearch:
        async def research(self, *_args, **_kwargs):
            return {
                "status": "ok",
                "company_report": company_report,
                "company_from_cache": False,
                "position_report": position_report,
                "anchor": company_report["anchor"],
                "planner": "model",
                "web_question_candidates": [
                    {"text": "触发下游", "source_url": "https://example.com/job"}
                ],
                "company_report_generated_time": NOW.isoformat(),
                "position_report_generated_time": NOW.isoformat(),
                "semantic_claim": _semantic_claim(),
            }

    result = asyncio.run(
        PrepService(db_path, CompletedResearch()).run(
            "u1", 1, generation="generation-1"
        )
    )

    assert result["status"] == "ok"
    detail = applications.application_detail(db_path, "u1", 1)
    assert detail["prep_status"] == "ready"
    assert detail["prep"]["localized"]["zh-CN"]["research_snapshot"]["snapshot_version"] == 3
    assert detail["prep"]["research_attempt"]["attempt_state"] == "succeeded"
