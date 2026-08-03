
import json
from datetime import date

import pytest

from careerdesk.platform.database import init_db, now_iso, transaction
from careerdesk.features.personal_state.public import build_personal_state_queries
from careerdesk.agentic.tools import (PreferencesTool, QueryGrillTool, QueryLibraryTool,
                                     QueryPrepTool, QueryStatusTool, QueryStudyTool,
                                     QueryTimelineTool)
from careerdesk.features.resumes import repository as resume_repository


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    now = now_iso()
    with transaction(path) as conn:
        conn.execute("INSERT INTO applications (user_id, company, position, stage, current_step, "
                     "applied_date, created_time, updated_time) VALUES "
                     "('me', '腾讯控股', '后端工程师', 'interviewing', '一面', "
                     "'2026-07-01', ?, ?)", (now, now))
        conn.execute("INSERT INTO applications (user_id, company, position, stage, applied_date, "
                     "created_time, updated_time) VALUES "
                     "('me', '字节跳动', '前端工程师', 'applied', '2026-07-05', ?, ?)", (now, now))
        conn.execute(
            "INSERT INTO journal (user_id, kind, content, created_time, processed_time, state) "
            "VALUES ('me', 'review', '腾讯一面复盘', ?, ?, 'applied')",
            (now, now),
        )
        conn.execute(
            "INSERT INTO questions (user_id, text, source, company, application_id, category, "
            "channel, response_format, evaluation_kind, primary_competency, answer_guide_json, journal_id, "
            "created_time, updated_time) VALUES "
            "('me', 'prompt cache 怎么降成本？', 'real', '腾讯控股', 1, "
            "'professional_domain', 'interview', 'oral_text', 'rubric', '成本优化', "
            "'{\"summary\":\"合并前缀、批量命中。\"}', 1, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO questions (user_id, text, source, category, channel, response_format, "
            "evaluation_kind, primary_competency, created_time, updated_time) VALUES "
            "('me', 'alpha 的入口设计？', 'imported', 'resume_deep_dive', 'interview', "
            "'oral_text', 'rubric', '系统设计', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO review_question_occurrences (user_id, journal_id, question_id, "
            "application_id, company, source_step, asked_date) VALUES "
            "('me', 1, 1, 1, '腾讯控股', '一面', '2026-07-01')",
        )
        conn.execute("INSERT INTO knowledge_points (user_id, name, box, created_time, updated_time) "
                     "VALUES ('me', 'prompt cache', 0, ?, ?)", (now, now))
        conn.execute("INSERT INTO knowledge_points (user_id, name, box, created_time, updated_time) "
                     "VALUES ('me', 'RAG', 4, ?, ?)", (now, now))
        conn.execute("INSERT INTO question_knowledge (question_id, knowledge_point_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO competency_progress (user_id, scope_kind, scope_ref, context_label, "
            "competency_key, box, practice_count, last_verdict, created_time, updated_time) "
            "VALUES ('me', 'global', 'global', '全局', 'prompt cache', 0, 1, 'needs_work', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO competency_progress (user_id, scope_kind, scope_ref, context_label, "
            "competency_key, box, practice_count, last_verdict, created_time, updated_time) "
            "VALUES ('me', 'global', 'global', '全局', 'RAG', 4, 3, 'meets', ?, ?)",
            (now, now),
        )
        research = json.dumps({"business": "混元大模型", "culture": "正直进取",
                               "recent_news": "Hy3 开源", "interview_style": "抠原理",
                               "likely_questions": ["为什么选腾讯？"]}, ensure_ascii=False)
        conn.execute("INSERT INTO companies (user_id, name, research_json, research_time, "
                     "created_time, updated_time) VALUES ('me', '腾讯控股', ?, ?, ?, ?)",
                     (research, now, now, now))
        conn.execute(
            "INSERT INTO question_sets (user_id, kind, state, stage, generation, "
            "material_fingerprint, policy_fingerprint, generation_fingerprint, prompt_version, "
            "schema_version, rubric_version, segmentation_version, summary_policy_version, "
            "input_receipt_json, coverage_json, context_label, finished_time, created_time, updated_time) "
            "VALUES ('me', 'library_snapshot', 'ready', 'ready', 'fixture-generation', ?, ?, ?, "
            "'fixture-prompt', 'fixture-schema', 'fixture-rubric', 'fixture-segments', "
            "'fixture-summary', '{}', '{}', '腾讯岗位快照', ?, ?, ?)",
            ("1" * 64, "2" * 64, "3" * 64, now, now, now),
        )
        item_sql = (
            "INSERT INTO question_set_items (user_id, question_set_id, ordinal, "
            "canonical_question_id, canonical_revision, canonical_digest, text, category, channel, "
            "response_format, evaluation_kind, difficulty, primary_competency, secondary_tags_json, "
            "rubric_json, answer_authority, answer_guide_json, evidence_json, follow_up_allowed, "
            "repeat_scope, created_time) VALUES ('me', 1, ?, ?, 1, ?, ?, ?, 'interview', "
            "'oral_text', 'rubric', 'intermediate', ?, '[]', '{}', 'user_verified', ?, '[]', 0, "
            "'global', ?)"
        )
        conn.execute(item_sql, (0, 1, "4" * 64, "prompt cache 怎么降成本？",
                                "professional_domain", "成本优化",
                                '{"summary":"合并前缀、批量命中。"}', now))
        conn.execute(item_sql, (1, 2, "5" * 64, "alpha 的入口设计？",
                                "resume_deep_dive", "系统设计", '{}', now))
        summary = json.dumps({"total": 2, "pass": 1, "partial": 0, "fail": 0, "skipped": 1}, ensure_ascii=False)
        conn.execute("INSERT INTO grill_sessions (user_id, question_set_id, kind, context_label, "
                     "state, plan_json, summary_json, started_time, ended_time, updated_time) VALUES "
                     "('me', 1, 'library_snapshot', '腾讯岗位快照', 'finished', "
                     "'{\"total\":2}', ?, ?, ?, ?)", (summary, now, now, now))
        conn.execute("INSERT INTO grill_sessions (user_id, question_set_id, kind, context_label, "
                     "state, plan_json, started_time, updated_time) VALUES "
                     "('me', 1, 'library_snapshot', '通用题库快照', 'suspended', "
                     "'{\"total\":2}', ?, ?)", (now, now))
        conn.execute("INSERT INTO grill_session_items (user_id, session_id, question_set_item_id, "
                     "ordinal, state) VALUES ('me', 1, 1, 0, 'answered')")
        conn.execute("INSERT INTO grill_session_items (user_id, session_id, question_set_item_id, "
                     "ordinal, state) VALUES ('me', 1, 2, 1, 'skipped')")
        conn.execute("INSERT INTO grill_answers (user_id, session_id, session_item_id, question_id, "
                     "verdict, feedback, created_time) VALUES "
                     "('me', 1, 1, 1, 'meets', "
                     "'{\"strengths\":[\"能说明缓存命中\"],\"gaps\":[\"缺少失效策略\"],"
                     "\"next_step\":\"补充一致性方案\"}', ?)", (now,))
        conn.execute("INSERT INTO grill_answers (user_id, session_id, session_item_id, question_id, "
                     "verdict, created_time) VALUES ('me', 1, 2, 2, 'skipped', ?)", (now,))
    return path


def test_query_grill_stats_history_suspended(db_path):
    tool = QueryGrillTool(db_path, "me")
    stats = tool.run({"action": "stats"})
    assert "共 2 场" in stats.text and "2 道作答" in stats.text
    assert stats.data["by_state"]["finished"] == 1 and stats.data["by_state"]["suspended"] == 1
    assert stats.data["verdicts"]["meets"] == 1 and stats.data["verdicts"]["skipped"] == 1
    腾讯 = tool.run({"action": "stats", "context": "腾讯"})
    assert 腾讯.data["total_sessions"] == 1
    assert len(tool.run({"action": "history"}).data["items"]) == 1
    assert len(tool.run({"action": "suspended"}).data["items"]) == 1
    assert len(tool.run({"action": "history", "context": "通用"}).data["items"]) == 0
    detail = tool.run({"action": "session_detail", "session_id": 1})
    assert detail.data["session"]["answers"][0]["feedback"]["gaps"] == ["缺少失效策略"]
    assert detail.data["ui_actions"] == [{"kind": "open_grill_session", "resource_id": 1}]


def test_query_timeline_list_and_fuzzy_company(db_path):
    tool = QueryTimelineTool(db_path, "me")
    all_list = tool.run({"action": "list"})
    assert "共 2 条" in all_list.text
    interviewing = tool.run({"action": "list", "stage": "interviewing"})
    assert "共 1 条" in interviewing.text and "腾讯控股" in interviewing.text
    fuzzy = tool.run({"action": "company", "company": "字节"})
    assert "字节跳动" in fuzzy.text
    miss = tool.run({"action": "company", "company": "美团"})
    assert "投过" in miss.text and "腾讯控股" in miss.text


def test_query_timeline_company_tolerates_delete_between_search_and_detail(
    db_path,
    monkeypatch,
):
    from careerdesk.agentic.tools import query_timeline as query_timeline_module

    monkeypatch.setattr(
        query_timeline_module.applications,
        "find_applications_by_company",
        lambda *_args, **_kwargs: [{"id": 1}],
    )
    monkeypatch.setattr(
        query_timeline_module.applications,
        "application_detail",
        lambda *_args, **_kwargs: None,
    )

    result = QueryTimelineTool(db_path, "me").run({
        "action": "company", "company": "腾讯",
    })

    assert result.status == "success"
    assert result.data == {"matches": []}
    assert "已被删除或合并" in result.text


def test_query_timeline_upcoming_is_exactly_the_requested_calendar_window(
    db_path,
    monkeypatch,
):
    from careerdesk.agentic.tools import query_timeline as query_timeline_module

    monkeypatch.setattr(query_timeline_module, "local_today", lambda: date(2026, 7, 7))
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE applications SET next_stage='interviewing', next_step='二面', "
            "next_date='2026-07-13' WHERE company='腾讯控股'",
        )
        conn.execute(
            "UPDATE applications SET next_stage='interviewing', next_step='一面', "
            "next_date='2026-07-14' WHERE company='字节跳动'",
        )

    tool = QueryTimelineTool(db_path, "me")
    result = tool.run({"action": "upcoming", "days": 7})

    assert [item["company"] for item in result.data["items"]] == ["腾讯控股"]
    assert "未来 7 天" in result.text
    assert tool.run({"action": "upcoming", "days": 0}).status == "error"
    assert tool.run({"action": "upcoming", "days": 61}).status == "error"


def test_query_timeline_history_attention_and_statistics(db_path, monkeypatch):
    from careerdesk.agentic.tools import query_timeline as query_timeline_module

    monkeypatch.setattr(query_timeline_module, "local_today", lambda: date(2026, 7, 21))
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO timeline_entries (user_id, application_id, from_stage, to_stage, "
            "step, summary, occurred_date, source, created_time) VALUES "
            "('me', 1, 'interviewing', 'interviewing', '一面', "
            "'完成技术一面', '2026-07-10', 'manual', ?)",
            (now_iso(),),
        )
        conn.execute(
            "UPDATE applications SET created_time='2026-05-01T00:00:00+00:00', "
            "updated_time='2026-05-01T00:00:00+00:00', "
            "next_step=NULL, next_date=NULL, channel='官网' WHERE id=2",
        )

    tool = QueryTimelineTool(db_path, "me")
    history = tool.run({
        "action": "history", "company": "腾讯", "position": "后端工程师", "limit": 1,
    })
    assert history.data["entries"][0]["summary"] == "完成技术一面"
    assert history.data["ui_actions"][0]["resource_id"] == 1

    attention = tool.run({"action": "attention", "days": 30})
    assert attention.data["items"][0]["company"] == "字节跳动"
    assert "没有新增事实" in attention.data["items"][0]["attention_reason"]

    statistics = tool.run({"action": "statistics", "channel": "官网"})
    assert statistics.data["total"] == 1
    assert statistics.data["by_stage"] == {"applied": 1}
    assert statistics.data["ui_actions"][0]["resource_id"] == 2


def test_query_study_knowledge_and_filtered_overview(db_path):
    tool = QueryStudyTool(db_path, "me")
    dist = tool.run({"action": "knowledge"})
    assert len(dist.data["aggregate"]) == 2
    assert {item["box"] for item in dist.data["scopes"]} == {0, 4}
    single = tool.run({"action": "knowledge", "name": "prompt"})
    assert single.data == [{
        "name": "prompt cache", "box": 0, "practice_count": 1,
        "last_asked_time": None,
    }]
    overview = tool.run({"action": "overview", "company": "腾讯", "source": "real"})
    assert overview.data["total"] == 1 and overview.data["by_source"]["real"]["practiced"] == 1
    listed = tool.run({"action": "questions", "company": "腾讯"})
    assert listed.data["items"][0]["answer_guide"]["summary"].startswith("合并前缀")


def test_query_library_bounds_many_resumes_without_returning_full_text(db_path):
    for index in range(55):
        marker = f"private-full-text-{index}"
        resume_repository.upsert_resume(
            db_path,
            "me",
            f"版本-{index:02d}",
            marker,
            family="backend",
            lines=[{
                "text": f"line-{line}-" + "x" * 600,
                "knowledge_points": ["Python"] if line == 0 else [],
            } for line in range(10)],
            overwrite_existing=False,
        )

    tool = QueryLibraryTool(db_path, "me")
    inventory = tool.run({"action": "resume_inventory"})
    assert inventory.data["count"] == 55
    assert len(inventory.data["items"]) == 50
    assert inventory.data["truncated"] is True
    assert {item["tag"] for item in inventory.data["items"]} == {"通用版"}
    assert "另有 5 份未展开" in inventory.text
    assert "private-full-text" not in inventory.text

    recent = tool.run({"action": "resumes"})
    assert len(recent.data["items"]) == 5
    assert recent.data["truncated"] is True
    assert all("content_text" not in item for item in recent.data["items"])
    assert all(len(item["lines"]) == 8 for item in recent.data["items"])
    assert all(len(line["text"]) <= 501 for item in recent.data["items"]
               for line in item["lines"])

    exact = tool.run({"action": "resumes", "name": "版本-00", "limit": 20})
    assert [item["name"] for item in exact.data["items"]] == ["版本-00"]
    assert exact.data["truncated"] is False


def test_query_prep_company_research(db_path):
    tool = QueryPrepTool(db_path, "me")
    prep = tool.run({"action": "company", "company": "腾讯"})
    assert "混元大模型" in prep.text and "抠原理" in prep.text
    briefing = tool.run({
        "action": "briefing", "company": "腾讯", "position": "后端 工程师",
    })
    assert briefing.status == "success" and "没有公司名含" not in briefing.text
    assert tool.run({
        "action": "briefing", "company": "腾讯", "position": 42,
    }).status == "error"
    assert tool.run({"action": "company", "company": 42}).status == "error"


def test_query_prep_reads_existing_resume_adaptation_without_generation(db_path, monkeypatch):
    from careerdesk.agentic.tools import query_prep as module

    monkeypatch.setattr(module, "inspect_resume_adaptation", lambda *_args: {
        "state": "ok",
        "report": {
            "fit_band": "medium",
            "summary_sentences": ["后端经验基本匹配"],
            "overall_advice": [{"action": "突出缓存项目", "reason": "岗位强调性能"}],
            "major_gaps": [{"requirement_summary": "高并发", "basis": "简历证据不足"}],
            "next_steps": ["补充可量化指标"],
        },
        "host_limitations": ["只基于当前简历和 JD"],
    })

    result = QueryPrepTool(db_path, "me").run({
        "action": "resume_adaptation", "company": "腾讯", "position": "后端工程师",
    })

    assert "后端经验基本匹配" in result.text
    assert result.data["fit_band"] == "medium"
    assert result.data["ui_actions"] == [
        {"kind": "open_resume_adaptation", "resource_id": 1},
    ]


def test_query_status_empty_is_graceful(db_path):
    result = QueryStatusTool(build_personal_state_queries(db_path), "me").run({"action": "patterns"})
    assert result.status == "success" and "还看不出" in result.text


def test_preferences_delete_checks_existence(db_path):
    creator = PreferencesTool(
        db_path, "me", client_turn_id="00000000-0000-4000-8000-000000000201",
    )
    creator.run({
        "action": "apply",
        "changes": [{"op": "set", "key": "薪资底线", "value": "25k"}],
    })
    miss = PreferencesTool(
        db_path, "me", client_turn_id="00000000-0000-4000-8000-000000000202",
    ).run({
        "action": "apply",
        "changes": [{"op": "delete", "key": "薪资"}],
    })
    assert miss.status == "partial" and "list" in miss.text and "0 项" not in miss.text
    hit = PreferencesTool(
        db_path, "me", client_turn_id="00000000-0000-4000-8000-000000000203",
    ).run({
        "action": "apply",
        "changes": [{"op": "delete", "key": "薪资底线"}],
    })
    assert hit.status == "success"
