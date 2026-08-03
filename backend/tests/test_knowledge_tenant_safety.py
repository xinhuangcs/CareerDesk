
import pytest

from careerdesk.platform.database import init_db, read_connection, transaction
from careerdesk.features.knowledge import public as knowledge


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "careerdesk.db")
    init_db(path)
    return path


def _seed_question_and_knowledge(conn, user_id: str) -> tuple[int, int]:
    question_id = conn.execute(
        "INSERT INTO questions (user_id, text, source, created_time, updated_time) "
        "VALUES (?, ?, 'generated', 'created', 'updated')",
        (user_id, f"{user_id}-question"),
    ).lastrowid
    knowledge_point_id = knowledge.touch_knowledge_point_in_transaction(
        conn, user_id, f"{user_id}-knowledge", stuck=False,
    )
    return question_id, knowledge_point_id


def test_link_accepts_only_ids_owned_by_the_declared_user(db_path):
    with transaction(db_path) as conn:
        u1_question, u1_knowledge = _seed_question_and_knowledge(conn, "u1")
        u2_question, u2_knowledge = _seed_question_and_knowledge(conn, "u2")

    with transaction(db_path) as conn:
        assert conn.in_transaction is False
        knowledge.link_question_knowledge_in_transaction(
            conn, "u1", u1_question, u1_knowledge,
        )
        knowledge.link_question_knowledge_in_transaction(
            conn, "u1", u1_question, u1_knowledge,
        )
        assert conn.in_transaction is True

    with transaction(db_path) as conn:
        assert conn.in_transaction is False
        for user_id, question_id, knowledge_point_id in (
            ("u1", u1_question, u2_knowledge),
            ("u1", u2_question, u1_knowledge),
            ("u2", u1_question, u1_knowledge),
        ):
            with pytest.raises(ValueError, match="same user"):
                knowledge.link_question_knowledge_in_transaction(
                    conn, user_id, question_id, knowledge_point_id,
                )
        assert conn.in_transaction is True

    with read_connection(db_path) as conn:
        links = conn.execute(
            "SELECT q.user_id, kp.user_id FROM question_knowledge qk "
            "JOIN questions q ON q.id = qk.question_id "
            "JOIN knowledge_points kp ON kp.id = qk.knowledge_point_id",
        ).fetchall()
    assert links == [("u1", "u1")]


def test_invalid_cross_tenant_link_rolls_back_the_callers_whole_transaction(db_path):
    with transaction(db_path) as conn:
        _u1_question, u1_knowledge = _seed_question_and_knowledge(conn, "u1")
        _u2_question, u2_knowledge = _seed_question_and_knowledge(conn, "u2")

    with pytest.raises(ValueError, match="same user"):
        with transaction(db_path) as conn:
            new_question = conn.execute(
                "INSERT INTO questions (user_id, text, source, created_time, updated_time) "
                "VALUES ('u1', 'must-roll-back', 'generated', 'created', 'updated')",
            ).lastrowid
            knowledge.link_question_knowledge_in_transaction(
                conn, "u1", new_question, u2_knowledge,
            )

    with read_connection(db_path) as conn:
        assert conn.execute(
            "SELECT id FROM questions WHERE text = 'must-roll-back'",
        ).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM question_knowledge").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM knowledge_points WHERE id IN (?, ?)",
            (u1_knowledge, u2_knowledge),
        ).fetchone()[0] == 2
