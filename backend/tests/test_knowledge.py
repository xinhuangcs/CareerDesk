
import pytest

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.features.knowledge import public as knowledge
from careerdesk.features.knowledge import repository


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    return path


@pytest.mark.parametrize(
    ("stuck", "replay", "ticks", "expected"),
    [
        (False, False, ["asked", "created", "updated"],
         (0, 0, "asked", None, "created", "updated")),
        (True, False, ["asked", "wrong", "created", "updated"],
         (0, 0, "asked", "wrong", "created", "updated")),
        (True, True, ["asked", "created", "updated"],
         (0, 0, "asked", None, "created", "updated")),
    ],
)
def test_touch_new_knowledge_preserves_stuck_and_replay_projection(
    db_path,
    monkeypatch,
    stuck,
    replay,
    ticks,
    expected,
):
    clock = iter(ticks)
    monkeypatch.setattr(repository, "now_iso", lambda: next(clock))

    with transaction(db_path) as conn:
        knowledge_point_id = knowledge.touch_knowledge_point_in_transaction(
            conn,
            "u1",
            f"knowledge-{stuck}-{replay}",
            stuck=stuck,
            replay=replay,
        )

    with read_connection(db_path) as conn:
        row = conn.execute(
            "SELECT box, correct_streak, last_asked_time, last_wrong_time, "
            "created_time, updated_time FROM knowledge_points WHERE id = ?",
            (knowledge_point_id,),
        ).fetchone()
    assert row == expected


def test_touch_existing_knowledge_preserves_update_and_replay_rules(db_path, monkeypatch):
    with transaction(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO knowledge_points "
            "(user_id, name, box, correct_streak, last_asked_time, last_wrong_time, due_date, "
            "created_time, updated_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "u1", "事务", 4, 3, "asked-old", "wrong-old", "2099-01-01",
                "created-old", "updated-old",
            ),
        )
        knowledge_point_id = cursor.lastrowid

    clock = iter(["asked-new", "updated-new"])
    monkeypatch.setattr(repository, "now_iso", lambda: next(clock))
    with transaction(db_path) as conn:
        assert knowledge.touch_knowledge_point_in_transaction(
            conn, "u1", "事务", stuck=False,
        ) == knowledge_point_id

    with read_connection(db_path) as conn:
        after_touch = conn.execute(
            "SELECT box, correct_streak, last_asked_time, last_wrong_time, due_date, updated_time "
            "FROM knowledge_points WHERE id = ?",
            (knowledge_point_id,),
        ).fetchone()
    assert after_touch == (4, 3, "asked-new", "wrong-old", "2099-01-01", "updated-new")

    def unexpected_clock_call():
        raise AssertionError("replay must not mutate or read the clock")

    monkeypatch.setattr(repository, "now_iso", unexpected_clock_call)
    with transaction(db_path) as conn:
        assert knowledge.touch_knowledge_point_in_transaction(
            conn, "u1", "事务", stuck=True, replay=True,
        ) == knowledge_point_id

    with read_connection(db_path) as conn:
        after_replay = conn.execute(
            "SELECT box, correct_streak, last_asked_time, last_wrong_time, due_date, updated_time "
            "FROM knowledge_points WHERE id = ?",
            (knowledge_point_id,),
        ).fetchone()
    assert after_replay == after_touch

    clock = iter(["wrong-stuck", "asked-stuck", "updated-stuck"])
    monkeypatch.setattr(repository, "now_iso", lambda: next(clock))
    with transaction(db_path) as conn:
        assert knowledge.touch_knowledge_point_in_transaction(
            conn, "u1", "事务", stuck=True,
        ) == knowledge_point_id

    with read_connection(db_path) as conn:
        after_stuck = conn.execute(
            "SELECT box, correct_streak, last_asked_time, last_wrong_time, due_date, updated_time "
            "FROM knowledge_points WHERE id = ?",
            (knowledge_point_id,),
        ).fetchone()
    assert after_stuck == (
        0, 0, "asked-stuck", "wrong-stuck", None, "updated-stuck",
    )


def test_link_is_idempotent_and_both_helpers_obey_outer_transaction(db_path):
    with transaction(db_path) as conn:
        question_id = conn.execute(
            "INSERT INTO questions (user_id, text, source, created_time, updated_time) "
            "VALUES ('u1', '题目', 'generated', 'created', 'updated')",
        ).lastrowid
        knowledge_point_id = knowledge.touch_knowledge_point_in_transaction(
            conn, "u1", "幂等", stuck=False,
        )
        knowledge.link_question_knowledge_in_transaction(
            conn, "u1", question_id, knowledge_point_id,
        )
        knowledge.link_question_knowledge_in_transaction(
            conn, "u1", question_id, knowledge_point_id,
        )

    with read_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM question_knowledge").fetchone()[0] == 1

    with pytest.raises(RuntimeError, match="roll back"):
        with transaction(db_path) as conn:
            rolled_back_question_id = conn.execute(
                "INSERT INTO questions (user_id, text, source, created_time, updated_time) "
                "VALUES ('u2', '回滚题', 'generated', 'created', 'updated')",
            ).lastrowid
            rolled_back_knowledge_id = knowledge.touch_knowledge_point_in_transaction(
                conn, "u2", "回滚知识点", stuck=True,
            )
            knowledge.link_question_knowledge_in_transaction(
                conn, "u2", rolled_back_question_id, rolled_back_knowledge_id,
            )
            raise RuntimeError("roll back")

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM questions WHERE user_id = 'u2'",
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM knowledge_points WHERE user_id = 'u2'",
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM question_knowledge").fetchone()[0] == 1
