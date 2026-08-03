
import asyncio
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from careerdesk.platform.database import init_db, now_iso, read_connection, transaction
from careerdesk.platform.ai.client import build_llm
from careerdesk.core.config import get_settings
from tests.review_record_test_helpers import execute_review_record

RUN_LLM_SMOKE = os.environ.get("RUN_LLM_SMOKE") == "1"
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
MODEL = os.environ.get("APP_LLM_MODEL")

pytestmark = pytest.mark.skipif(
    not (RUN_LLM_SMOKE and MODEL),
    reason="真实冒烟需显式设置 RUN_LLM_SMOKE=1，并配置 APP_LLM_MODEL 与对应 key",
)

REVIEW_TEXT = (
    "今天下午面了字节二面，Seed 的 LLM 应用岗。问了 RRF 为什么用排名不用分数，"
    "还追问了检查点恢复怎么保证幂等，第二个我卡了。前一晚没睡好状态一般。他说一周内出结果。"
)

JOBS_TEXT = "投了两个：米哈游 AI工程师（官网，7/20 截止笔试）；腾讯 应用研究-大模型方向（内推）。"


def run(coroutine):
    return asyncio.run(coroutine)


def configured_llm():
    """Build the selected model with the same explicit capacity contract as production."""
    settings = get_settings()
    return build_llm(
        MODEL,
        strict_offline=False,
        context_window=settings.llm_context_window,
        max_output_tokens=settings.llm_max_output_tokens,
    )


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "smoke.db")
    init_db(path)
    return path


def test_real_review_extraction_end_to_end(db_path):
    from careerdesk.features.reviews.public import ReviewService

    service = ReviewService(db_path, configured_llm())
    result = run(execute_review_record(service, "u1", REVIEW_TEXT))
    assert result["state"] == "completed"
    assert result["outcome"] in ("applied", "needs_clarification")
    assert (result["result"]["extraction"]["company"] or "").startswith("字节")
    if result["outcome"] == "applied":
        with read_connection(db_path) as conn:
            (questions,) = conn.execute("SELECT COUNT(*) FROM questions WHERE source='real'").fetchone()
            (timeline_entries,) = conn.execute(
                "SELECT COUNT(*) FROM timeline_entries"
            ).fetchone()
        assert questions >= 1 and timeline_entries == 1


def test_real_grill_judge_returns_valid_verdict(db_path):
    from careerdesk.features.grill.service import GrillService
    from tests.support.question_sets import seed_question_set

    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO questions (user_id, text, source, category, channel, response_format, "
            "evaluation_kind, primary_competency, rubric_json, answer_guide_json, "
            "answer_verification_json, created_time, updated_time) VALUES "
            "('u1', '从检查点恢复时怎么保证任务不被重复执行？', 'imported', "
            "'professional_domain', 'interview', 'oral_text', 'rubric', '检查点幂等', "
            "'{\"criteria\":[\"幂等写入\",\"完成集合去重\"]}', "
            "'{\"summary\":\"恢复前比对已完成集合；写入走幂等 upsert\"}', "
            "'{\"version\":\"answer-verification-v1\"}', ?, ?)",
            (now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO knowledge_points (user_id, name, box, correct_streak, created_time, updated_time) "
            "VALUES ('u1', '检查点幂等', 0, 0, ?, ?)", (now_iso(), now_iso()))
        conn.execute("INSERT INTO question_knowledge (question_id, knowledge_point_id) VALUES (?, 1)",
                     (cursor.lastrowid,))

    question_set_id = seed_question_set(db_path, "u1", [cursor.lastrowid])
    service = GrillService(db_path, configured_llm())
    started = service.start("u1", question_set_id=question_set_id, question_count=5)
    assert started["status"] == "ok"
    result = run(service.answer("u1", started["session_id"],
                                "恢复时先读已完成步骤的集合做去重，落库全部走带唯一键的 upsert，重复执行是空操作。",
                                session_item_id=started["question"]["id"]))
    assert result["status"] in ("ok", "finished")
    with read_connection(db_path) as conn:
        row = conn.execute("SELECT verdict, feedback FROM grill_answers").fetchone()
    if row is not None:
        verdict, feedback = row
        assert verdict in ("pass", "partial", "fail")
        assert feedback


def test_real_batch_parse_previews_positions(db_path):
    from careerdesk.features.applications.public import ApplicationService

    service = ApplicationService(db_path, configured_llm())
    result = run(service.parse_batch("u1", JOBS_TEXT))
    assert result["status"] == "preview"
    companies = {item["company"] for item in result["positions"]}
    assert len(result["positions"]) >= 2
    assert any("米哈游" in company for company in companies)
    assert any("腾讯" in company for company in companies)
    with read_connection(db_path) as conn:
        (applications,) = conn.execute("SELECT COUNT(*) FROM applications").fetchone()
    assert applications == 0
