
import asyncio
import copy
import json

import pytest
from tests.support import ScriptedLLM

from careerdesk.features.applications.repository import board as application_board
from careerdesk.features.reviews.public import ReviewExtractionUnavailable, ReviewService
from careerdesk.features.reviews.service import (
    _batch_identity_prompt_for,
    _batch_prompt_for,
    _prompt_for,
)
from careerdesk.orchestration.maintenance.service import MaintenanceService
from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.platform.database.connection import set_meta
from tests.review_record_test_helpers import execute_review_record

TODAY = "2026-07-07"


def test_review_prompts_use_native_frozen_output_locale():
    english = _prompt_for(TODAY, "en")
    chinese = _prompt_for(TODAY, "zh-CN")
    batch_english = _batch_prompt_for(TODAY, "en")
    identity_english = _batch_identity_prompt_for("en")

    assert "Today is 2026-07-07 (Tuesday)" in english
    assert "今天是 2026-07-07（星期二）" in chinese
    assert "Batch rules:" in batch_english
    assert "List role identities" in identity_english
    assert "统一状态模型" not in english


def test_batch_identity_extraction_is_bounded_by_the_extraction_deadline(db_path, monkeypatch):
    from careerdesk.features.reviews import service as review_service_module

    class HangingLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        review_service_module,
        "REVIEW_EXTRACTION_DEADLINE_SECONDS",
        0.2,
    )

    with pytest.raises(ReviewExtractionUnavailable) as captured:
        run(ReviewService(db_path, HangingLLM())._extract_batch("昨天字节一面通过", TODAY))
    assert captured.value.reason == "timeout"
    assert captured.value.phase == "batch_identity"


BATCH_DEADLINE_MANIFEST = {"items": [{
    "source_text": "字节后端",
    "company": "字节",
    "position": "后端",
}]}
BATCH_DEADLINE_EXTRACTION = {
    "company": "字节",
    "position": "后端",
    "projected_state": {"stage": "interviewing", "current_step": "一面"},
}


def test_targeted_batch_items_report_which_item_exhausted_the_deadline(db_path, monkeypatch):
    from careerdesk.features.reviews import service as review_service_module

    class ManifestThenHangingLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            if self.calls == 0:
                return self._next()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        review_service_module,
        "REVIEW_EXTRACTION_DEADLINE_SECONDS",
        0.2,
    )
    llm = ManifestThenHangingLLM([json.dumps(BATCH_DEADLINE_MANIFEST, ensure_ascii=False)])

    with pytest.raises(ReviewExtractionUnavailable) as captured:
        run(ReviewService(db_path, llm)._extract_batch("昨天字节后端一面通过", TODAY))
    assert captured.value.reason == "timeout"
    assert captured.value.phase == "batch_item_0"


def test_batch_phases_share_one_budget_instead_of_restarting_it_per_call(db_path, monkeypatch):
    from careerdesk.features.reviews import service as review_service_module

    class SlowLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            await asyncio.sleep(0.4)
            return self._next()

    monkeypatch.setattr(
        review_service_module,
        "REVIEW_EXTRACTION_DEADLINE_SECONDS",
        0.6,
    )
    llm = SlowLLM([
        json.dumps(BATCH_DEADLINE_MANIFEST, ensure_ascii=False),
        json.dumps(BATCH_DEADLINE_EXTRACTION, ensure_ascii=False),
    ])

    with pytest.raises(ReviewExtractionUnavailable) as captured:
        run(ReviewService(db_path, llm)._extract_batch("昨天字节后端一面通过", TODAY))
    assert captured.value.reason == "timeout"
    assert captured.value.phase == "batch_item_0"


def test_single_extraction_shares_the_batch_extraction_deadline(db_path, monkeypatch):
    from careerdesk.features.reviews import service as review_service_module

    class HangingLLM(ScriptedLLM):
        async def chat(self, messages, *, tools=None, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        review_service_module,
        "REVIEW_EXTRACTION_DEADLINE_SECONDS",
        0.2,
    )

    with pytest.raises(ReviewExtractionUnavailable) as captured:
        run(ReviewService(db_path, HangingLLM())._extract("昨天字节一面通过", TODAY))
    assert captured.value.reason == "timeout"
    assert captured.value.phase == "single"


GOLDEN_TEXT = (
    "今天下午面了字节二面，Seed 的 LLM 应用岗。问了 RRF 为什么用排名不用分数，"
    "还追问了检查点恢复怎么保证幂等，第二个我卡了。面试官人挺好，但我下午状态不行，"
    "前一晚没睡好。他说一周内出结果。"
)

GOLDEN_EXTRACTION = {
    "company": "字节",
    "position": "LLM应用",
    "channel": None,
    "history": {
        "step": "二面",
        "date": TODAY,
        "outcome": None,
        "summary": "字节二面完成",
    },
    "projected_state": {"stage": "interviewing", "current_step": "二面"},
    "next_action": {
        "stage": "interviewing",
        "step": "等待结果",
        "date": "2026-07-14",
        "time": None,
        "note": "一周内出结果",
    },
    "questions": [
        {
            "text": "RRF 为什么用排名不用分数？",
            "stuck": False,
            "knowledge_points": ["RRF 融合"],
        },
        {
            "text": "检查点恢复怎么保证幂等？",
            "stuck": True,
            "knowledge_points": ["检查点幂等"],
        },
    ],
    "mood": "下午状态不佳",
    "time_of_day": "afternoon",
    "factors": ["睡眠差"],
}


def golden(**overrides) -> dict:
    extraction = copy.deepcopy(GOLDEN_EXTRACTION)
    extraction.update(overrides)
    return extraction


def history(
    step: str | None,
    occurred_date: str | None,
    *,
    outcome: str | None = None,
    summary: str | None = None,
) -> dict:
    return {
        "step": step,
        "date": occurred_date,
        "outcome": outcome,
        "summary": summary,
    }


def next_action(
    step: str,
    *,
    stage: str = "interviewing",
    date: str | None = None,
    time: str | None = None,
    note: str | None = None,
) -> dict:
    return {"stage": stage, "step": step, "date": date, "time": time, "note": note}


def quiet(**overrides) -> dict:
    values = {
        "questions": [],
        "mood": None,
        "time_of_day": None,
        "factors": [],
    }
    values.update(overrides)
    return golden(**values)


def scripted(*extractions) -> ScriptedLLM:
    return ScriptedLLM([
        json.dumps(extraction, ensure_ascii=False) for extraction in extractions
    ])


def run(coroutine):
    return asyncio.run(coroutine)


def count(db_path: str, sql: str, *params) -> int:
    with read_connection(db_path) as conn:
        return conn.execute(sql, params).fetchone()[0]


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def test_golden_review_derives_all_sinks(db_path):
    service = ReviewService(db_path, scripted(GOLDEN_EXTRACTION))
    result = run(execute_review_record(service, "u1", GOLDEN_TEXT, today=TODAY))
    assert result["state"] == "completed" and result["outcome"] == "applied"

    with read_connection(db_path) as conn:
        application = conn.execute(
            "SELECT company, stage, current_step, next_stage, next_step, next_date, "
            "next_time, next_note FROM applications WHERE user_id='u1'",
        ).fetchone()
        entry = conn.execute(
            "SELECT step, occurred_date, outcome, summary, from_stage, to_stage, source "
            "FROM timeline_entries WHERE user_id='u1'",
        ).fetchone()
        stuck_box, stuck_wrong_time = conn.execute(
            "SELECT box, last_wrong_time FROM knowledge_points WHERE name='检查点幂等'",
        ).fetchone()
        processed_time, derivation_json = conn.execute(
            "SELECT processed_time, derivation_json FROM journal WHERE id=?",
            (result["target_journal_id"],),
        ).fetchone()

    assert tuple(application) == (
        "字节", "interviewing", "二面", "interviewing", "等待结果",
        "2026-07-14", None, "一周内出结果",
    )
    assert tuple(entry) == (
        "二面", TODAY, None, "字节二面完成", "backlog", "interviewing", "review",
    )
    assert count(db_path, "SELECT COUNT(*) FROM questions WHERE source='real'") == 2
    assert count(db_path, "SELECT COUNT(*) FROM question_knowledge") == 2
    assert count(db_path, "SELECT COUNT(*) FROM status_log WHERE time_of_day='afternoon'") == 1
    assert stuck_box == 0 and stuck_wrong_time is not None
    assert processed_time is not None
    assert json.loads(derivation_json)["application_id"] is not None


def test_explicit_today_fills_only_missing_history_date(db_path):
    payload = golden(history=history("二面", None, summary="完成二面"))
    service = ReviewService(db_path, scripted(payload))
    result = run(execute_review_record(
        service, "u1", "今天字节 LLM 应用岗二面，问了 RRF。", today=TODAY,
    ))
    assert result["result"]["extraction"]["history"]["date"] == TODAY
    assert count(
        db_path,
        "SELECT COUNT(*) FROM timeline_entries WHERE occurred_date=?",
        TODAY,
    ) == 1


def test_uncertain_today_wording_does_not_invent_history_date(db_path):
    payload = golden(history=history("二面", None, summary="完成二面"))
    service = ReviewService(db_path, scripted(payload))
    result = run(execute_review_record(
        service,
        "u1",
        "今天聊的是字节 LLM 应用岗二面，实际面试日期稍后补充。",
        today=TODAY,
    ))
    assert result["result"]["extraction"]["history"]["date"] is None
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries WHERE occurred_date IS NULL") == 1


def test_explicit_compact_position_is_recovered_when_model_omits_it(db_path):
    service = ReviewService(db_path, scripted(golden(company="腾讯", position=None)))
    extracted = run(service._extract("我昨天面完腾讯后端一面", TODAY))
    assert extracted.position == "后端"


def test_ambiguous_compact_position_is_not_invented(db_path):
    service = ReviewService(db_path, scripted(golden(company="腾讯", position=None)))
    extracted = run(service._extract("我昨天面完腾讯那个岗一面", TODAY))
    assert extracted.position is None


@pytest.mark.parametrize(
    ("spoken", "canonical"),
    [
        ("我昨天在BOSS直聘上面投递了甲骨文的前端工程师岗位", "Boss直聘"),
        ("我昨天在 Indeed 上投递了 Stripe 的 Backend Engineer 岗位", "Indeed"),
    ],
)
def test_explicit_channel_is_recovered_and_persisted(db_path, spoken, canonical):
    company = "甲骨文" if canonical == "Boss直聘" else "Stripe"
    position = "前端工程师" if canonical == "Boss直聘" else "Backend Engineer"
    payload = quiet(
        company=company,
        position=position,
        channel=None,
        history=history("提交申请", "2026-07-06", summary="已投递"),
        projected_state={"stage": "applied", "current_step": "已提交"},
        next_action=None,
    )
    result = run(execute_review_record(
        ReviewService(db_path, scripted(payload)), "u1", spoken, today=TODAY,
    ))
    assert result["result"]["extraction"]["channel"] == canonical
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT channel, stage FROM applications WHERE user_id='u1'",
        ).fetchone() == (canonical, "applied")


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [("我今天刚刚通过了腾讯 Agent 开发的二面", "passed"), ("腾讯二面挂了", "failed")],
)
def test_explicit_interview_outcome_is_recovered_without_a_classification_field(
    db_path,
    spoken,
    expected,
):
    payload = quiet(
        company="腾讯",
        position="Agent开发",
        history=history("二面", None, summary="二面结束"),
        projected_state={"stage": "interviewing", "current_step": "二面"},
        next_action=None,
    )
    extracted = run(ReviewService(db_path, scripted(payload))._extract(spoken, TODAY))
    assert extracted.history is not None and extracted.history.outcome == expected


def test_passed_written_test_stays_in_written_test_until_interview_is_explicit(db_path):
    payload = quiet(
        company="字节",
        position="Agent开发",
        history=history("笔试", TODAY, outcome="passed", summary="笔试通过"),
        projected_state={"stage": "written_test", "current_step": "笔试"},
        next_action=next_action("等待通知", stage="written_test"),
    )
    result = run(execute_review_record(
        ReviewService(db_path, scripted(payload)),
        "u1",
        "字节 Agent 开发笔试通过了，等通知",
        today=TODAY,
    ))
    assert result["result"]["extraction"]["next_action"]["stage"] == "written_test"
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT stage, current_step FROM applications").fetchone() == (
            "written_test", "笔试",
        )


def test_compound_progress_keeps_fact_current_state_and_next_action_distinct(db_path):
    payload = quiet(
        company="腾讯",
        position="Agent开发",
        history=history("二面", TODAY, outcome="passed", summary="二面通过"),
        projected_state={"stage": "interviewing", "current_step": "二面"},
        next_action=next_action(
            "终面", date="2026-07-15", time="10:00", note="参加终面",
        ),
    )
    result = run(execute_review_record(
        ReviewService(db_path, scripted(payload)),
        "u1",
        "腾讯 Agent 开发二面过了，下周三上午十点终面",
        today=TODAY,
    ))
    extracted = result["result"]["extraction"]
    assert extracted["history"] == payload["history"]
    assert extracted["projected_state"] == payload["projected_state"]
    assert extracted["next_action"] == payload["next_action"]
    card = application_board(db_path, "u1")["columns"]["interviewing"][0]
    assert card["current_step"] == "二面"
    assert card["next_action"] == payload["next_action"]


def test_pooled_stage_retains_previous_stage_and_future_resume_is_not_current(db_path):
    initial = quiet(next_action=None)
    pooled = quiet(
        history=history("HC 冻结", TODAY, summary="暂时进入人才池"),
        projected_state={"stage": "pooled", "current_step": "HC 冻结"},
        next_action=None,
    )
    resumed = quiet(
        history=history("流程恢复", "2026-07-10", summary="流程已恢复"),
        projected_state={"stage": "interviewing", "current_step": "二面"},
        next_action=next_action("三面", date="2026-07-16", note="参加三面"),
    )
    service = ReviewService(db_path, scripted(initial, pooled, resumed))
    run(execute_review_record(service, "u1", "字节二面结束", today=TODAY))
    run(execute_review_record(service, "u1", "字节 HC 冻结，先放人才池", today=TODAY))
    paused = application_board(db_path, "u1")["columns"]["pooled"][0]
    assert paused["paused_from_stage"] == "interviewing"
    assert paused["current_step"] == "HC 冻结"

    run(execute_review_record(
        service, "u1", "流程恢复，7 月 16 日三面", today=TODAY,
    ))
    card = application_board(db_path, "u1")["columns"]["interviewing"][0]
    assert card["current_step"] == "二面"
    assert card["next_action"]["step"] == "三面"


def test_company_specific_future_step_stays_in_next_action(db_path):
    payload = quiet(
        company="DTU",
        position="TA",
        history=history(None, TODAY, summary="已收到安排"),
        projected_state={"stage": "interviewing", "current_step": None},
        next_action=next_action(
            "Assessment Center",
            date="2026-07-20",
            note="参加 Assessment Center",
        ),
    )
    run(execute_review_record(
        ReviewService(db_path, scripted(payload)),
        "u1",
        "DTU TA 安排了 7 月 20 日 Assessment Center",
        today=TODAY,
    ))
    card = application_board(db_path, "u1")["columns"]["interviewing"][0]
    assert card["current_step"] is None
    assert card["next_action"]["step"] == "Assessment Center"


def test_history_only_follow_up_does_not_overwrite_current_projection(db_path):
    first = quiet(next_action=None)
    follow_up = quiet(
        history=history(None, TODAY, summary="招聘方正在确认终面时间"),
        projected_state=None,
        next_action=next_action("等待终面时间"),
    )
    service = ReviewService(db_path, scripted(first, follow_up))
    run(execute_review_record(service, "u1", "字节二面完成", today=TODAY))
    run(execute_review_record(service, "u1", "招聘方正在确认终面时间", today=TODAY))
    card = application_board(db_path, "u1")["columns"]["interviewing"][0]
    assert card["current_step"] == "二面"
    assert card["next_action"]["step"] == "等待终面时间"
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT summary, from_step, to_step FROM timeline_entries ORDER BY id DESC LIMIT 1",
        ).fetchone() == ("招聘方正在确认终面时间", "二面", "二面")


def test_missing_step_and_date_remain_null_without_forced_supplement(db_path):
    payload = quiet(
        history=history(None, None, summary="完成了一次面试"),
        projected_state={"stage": "interviewing", "current_step": None},
        next_action=None,
    )
    done = run(execute_review_record(
        ReviewService(db_path, scripted(payload)),
        "u1",
        "面了字节 LLM 应用岗，具体环节和日期没说",
        today=TODAY,
    ))
    assert done["outcome"] == "applied" and done["result"]["missing"] == []
    assert count(
        db_path,
        "SELECT COUNT(*) FROM timeline_entries WHERE step IS NULL AND occurred_date IS NULL",
    ) == 1


def test_explicit_colloquial_position_does_not_guess_unique_company_application(db_path):
    second = quiet(
        position="Seed那个岗",
        history=history("三面", "2026-07-14", summary="三面完成"),
        projected_state={"stage": "interviewing", "current_step": "三面"},
        next_action=None,
    )
    service = ReviewService(db_path, scripted(GOLDEN_EXTRACTION, second))
    run(execute_review_record(service, "u1", GOLDEN_TEXT, today=TODAY))
    run(execute_review_record(
        service, "u1", "又面了字节 Seed 那个岗三面", today="2026-07-14",
    ))
    with read_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT position, current_step FROM applications ORDER BY id",
        ).fetchall()
    assert [tuple(row) for row in rows] == [("LLM应用", "二面"), ("Seed那个岗", "三面")]


def test_second_review_without_position_merges_into_unique_application(db_path):
    second = quiet(
        position=None,
        history=history("三面", "2026-07-14", summary="三面完成"),
        projected_state={"stage": "interviewing", "current_step": "三面"},
        next_action=None,
        questions=[{
            "text": "讲讲你项目里的权限模型？",
            "stuck": False,
            "knowledge_points": ["工具权限"],
        }],
    )
    service = ReviewService(db_path, scripted(GOLDEN_EXTRACTION, second))
    run(execute_review_record(service, "u1", GOLDEN_TEXT, today=TODAY))
    run(execute_review_record(
        service, "u1", "又面了字节三面，问了权限模型", today="2026-07-14",
    ))
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 2
    assert count(db_path, "SELECT COUNT(*) FROM questions") == 3
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT current_step FROM applications").fetchone() == ("三面",)


@pytest.mark.parametrize(
    ("first_company", "first_position", "second_company", "second_position"),
    [
        ("字节", "AI 应用工程师", "字节", "AI应用工程师"),
        ("字节 跳动", "后端", "字节跳动", "后端"),
    ],
)
def test_reviews_merge_identity_across_internal_spaces(
    db_path,
    first_company,
    first_position,
    second_company,
    second_position,
):
    first = quiet(company=first_company, position=first_position, next_action=None)
    second = quiet(
        company=second_company,
        position=second_position,
        history=history("三面", "2026-07-14", summary="三面完成"),
        projected_state={"stage": "interviewing", "current_step": "三面"},
        next_action=None,
    )
    service = ReviewService(db_path, scripted(first, second))
    run(execute_review_record(service, "u1", f"面了{first_company}{first_position}二面", today=TODAY))
    run(execute_review_record(
        service, "u1", f"又面了{second_company}{second_position}三面", today="2026-07-14",
    ))
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 2
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT company, position, current_step FROM applications",
        ).fetchone() == (first_company, first_position, "三面")


def test_explicit_offer_moves_application_stage_without_inventing_a_new_next_action(db_path):
    offer = quiet(
        history=history("收到 Offer", "2026-07-20", outcome="passed", summary="Offer 到手"),
        projected_state={"stage": "offer", "current_step": "收到 Offer"},
        next_action=None,
    )
    service = ReviewService(db_path, scripted(GOLDEN_EXTRACTION, offer))
    run(execute_review_record(service, "u1", GOLDEN_TEXT, today=TODAY))
    run(execute_review_record(service, "u1", "字节 offer 到手", today="2026-07-20"))
    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT stage, current_step, next_step FROM applications",
        ).fetchone() == ("offer", "收到 Offer", "等待结果")


def test_historical_fact_after_withdrawal_does_not_reopen_stage(db_path):
    withdrawn = quiet(
        history=history("主动放弃", "2026-07-20", outcome="cancelled", summary="不再跟进"),
        projected_state={"stage": "withdrawn", "current_step": "主动放弃"},
        next_action=None,
    )
    historical = quiet(
        history=history("三面", "2026-07-18", summary="补记三面"),
        projected_state=None,
        next_action=None,
    )
    service = ReviewService(db_path, scripted(GOLDEN_EXTRACTION, withdrawn, historical))
    run(execute_review_record(service, "u1", GOLDEN_TEXT, today=TODAY))
    run(execute_review_record(service, "u1", "我主动放弃了", today="2026-07-20"))
    run(execute_review_record(service, "u1", "补记 7 月 18 日三面", today="2026-07-21"))
    with read_connection(db_path) as conn:
        assert conn.execute("SELECT stage, current_step FROM applications").fetchone() == (
            "withdrawn", "主动放弃",
        )
        assert conn.execute(
            "SELECT to_stage, to_step FROM timeline_entries ORDER BY id DESC LIMIT 1",
        ).fetchone() == ("withdrawn", "主动放弃")


def test_ambiguous_position_retains_draft_for_clarification(db_path):
    with transaction(db_path) as conn:
        for position in ("LLM应用", "后端开发"):
            conn.execute(
                "INSERT INTO applications (user_id, company, position, created_time, updated_time) "
                "VALUES ('u1', '字节', ?, ?, ?)",
                (position, now_iso(), now_iso()),
            )
    payload = golden(position=None)
    result = run(execute_review_record(
        ReviewService(db_path, scripted(payload)), "u1", "面了字节二面", today=TODAY,
    ))
    assert result["outcome"] == "needs_clarification"
    assert [item["field"] for item in result["result"]["missing"]] == ["position"]
    assert "LLM应用" in result["result"]["missing"][0]["ask"]


def test_maintenance_reconciles_metadata_without_replaying_business_projection(db_path):
    run(execute_review_record(
        ReviewService(db_path, scripted(GOLDEN_EXTRACTION)),
        "u1",
        GOLDEN_TEXT,
        today=TODAY,
    ))
    set_meta(db_path, "derive_version:user:u1", 0)
    stats = run(MaintenanceService(db_path).reconcile("u1"))
    assert stats == {"status": "ok", "reconciled": 1}
    assert count(db_path, "SELECT COUNT(*) FROM timeline_entries") == 1
    assert count(db_path, "SELECT COUNT(*) FROM questions") == 2
    assert count(db_path, "SELECT COUNT(*) FROM applications") == 1
