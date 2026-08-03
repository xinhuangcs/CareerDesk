"""Grill state-machine regression tests for immutable question-set sessions."""

import asyncio

import pytest

from careerdesk.features.grill import repository
from careerdesk.features.grill.ai_models import JudgeVerdict
from careerdesk.features.grill.service import GrillService
from careerdesk.platform.database import init_db, now_iso, transaction
from tests.support.question_sets import seed_question_set


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    return path


def _set(db_path: str, count: int = 2) -> int:
    ids = []
    with transaction(db_path) as conn:
        for index in range(count):
            ids.append(conn.execute(
                "INSERT INTO questions (user_id, text, source, category, channel, response_format, "
                "evaluation_kind, primary_competency, rubric_json, answer_guide_json, created_time, "
                "updated_time) VALUES ('u1', ?, 'real', 'behavioral_situational', 'interview', "
                "'oral_text', 'rubric', '表达', ?, ?, ?, ?)",
                (f"题目 {index}", '{"essential_criteria":["具体"],"quality_signals":[],"critical_errors":[]}',
                 '{"kind":"guide","text":"具体回答"}', now_iso(), now_iso()),
            ).lastrowid)
    return seed_question_set(db_path, "u1", ids)


def test_start_freezes_actual_count_and_enforces_one_active_session(db_path):
    set_id = _set(db_path, 2)
    session_id, question, total = repository.create_session(
        db_path, "u1", question_set_id=set_id, question_count=20,
    )
    assert session_id > 0 and question["text"] == "题目 0" and total == 2
    with pytest.raises(ValueError, match="已有进行中场次"):
        repository.create_session(db_path, "u1", question_set_id=set_id, question_count=5)


def test_claim_failure_is_visible_and_retryable(db_path, monkeypatch):
    set_id = _set(db_path, 1)
    session_id, question, _ = repository.create_session(
        db_path, "u1", question_set_id=set_id, question_count=5,
    )

    async def fail(*_args, **_kwargs):
        raise RuntimeError("provider detail must not escape")

    monkeypatch.setattr("careerdesk.features.grill.service.judge_answer", fail)
    service = GrillService(db_path, object())
    result = asyncio.run(service.answer(
        "u1", session_id, "回答", session_item_id=question["id"],
    ))
    assert result == {"status": "error", "code": "judge_failed", "message": "判卷失败，请重试"}
    assert service.resume("u1", session_id)["code"] == "judge_failed"


def test_answer_advances_and_replay_uses_frozen_item(db_path, monkeypatch):
    set_id = _set(db_path, 1)
    session_id, question, _ = repository.create_session(
        db_path, "u1", question_set_id=set_id, question_count=5,
    )

    async def judge(*_args, **_kwargs):
        return JudgeVerdict(verdict="meets", stuck=False, strengths=["具体"], gaps=[],
                            next_step="保持", follow_up=None)

    monkeypatch.setattr("careerdesk.features.grill.service.judge_answer", judge)
    service = GrillService(db_path, object())
    assert asyncio.run(service.answer(
        "u1", session_id, "回答", session_item_id=question["id"],
    ))["status"] == "finished"
    replay = repository.replay(db_path, "u1", session_id)
    assert replay["answers"][0]["text"] == "题目 0"


def test_suspend_resume_delete_and_session_listing(db_path):
    set_id = _set(db_path, 1)
    service = GrillService(db_path, None)
    started = service.start("u1", question_set_id=set_id, question_count=5)
    session_id = started["session_id"]
    assert service.suspend("u1", session_id)["status"] == "suspended"
    assert repository.list_sessions(db_path, "u1", ["suspended"])[0]["answered"] == 0
    assert service.resume("u1", session_id)["status"] == "ok"
    assert repository.delete_session(db_path, "u1", session_id) is True
    assert repository.get_session(db_path, "u1", session_id) is None
