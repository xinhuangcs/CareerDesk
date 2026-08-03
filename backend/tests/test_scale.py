"""Bounded catalogue and immutable-set scale regressions."""

import pytest

from careerdesk.features.grill import repository as grill
from careerdesk.features.questions import repository
from careerdesk.platform.database import init_db, now_iso, transaction
from tests.support.question_sets import seed_question_set


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    return path


def _questions(db_path: str, count: int) -> list[int]:
    with transaction(db_path) as conn:
        return [conn.execute(
            "INSERT INTO questions (user_id, text, source, category, channel, response_format, "
            "evaluation_kind, primary_competency, rubric_json, answer_guide_json, created_time, "
            "updated_time) VALUES ('u1', ?, 'real', 'professional_domain', 'written', "
            "'short_written', 'rubric', '分析', ?, ?, ?, ?)",
            (f"规模题 {index}", '{"essential_criteria":["准确"],"quality_signals":[],"critical_errors":[]}',
             '{"kind":"guide","text":"说明依据"}', now_iso(), now_iso()),
        ).lastrowid for index in range(count)]


def test_session_preset_is_bounded_and_reports_actual_selected_count(db_path):
    ids = _questions(db_path, 7)
    set_id = seed_question_set(db_path, "u1", ids)
    _, _, total = grill.create_session(db_path, "u1", question_set_id=set_id, question_count=20)
    assert total == 7


def test_competency_overview_is_empty_without_gradable_scoped_answers(db_path):
    assert repository.competency_overview(db_path, "u1") == {"scopes": [], "aggregate": []}
