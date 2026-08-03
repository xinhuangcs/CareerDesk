"""Connection-level writes for knowledge identity, mastery, and question links.

Reviews call these inside cross-table transactions. This module never creates,
commits, rolls back, or closes the caller-owned connection.
"""

from ...platform.database import now_iso


def touch_knowledge_point_in_transaction(
    conn,
    user_id: str,
    name: str,
    *,
    stuck: bool,
    replay: bool = False,
) -> int:
    """Get or create a concept and reset real-interview misses to box zero.

    Box movement is deterministic: a real interview miss is the strongest failure
    signal; correct-answer promotion occurs in the Grill settlement transaction.
    During replay, only get-or-create so correcting history cannot erase later practice.

    Returns:
        int: Knowledge row ID.
    """
    row = conn.execute(
        "SELECT id FROM knowledge_points WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO knowledge_points (user_id, name, box, correct_streak, last_asked_time, "
            "last_wrong_time, created_time, updated_time) VALUES (?, ?, 0, 0, ?, ?, ?, ?)",
            (user_id, name, now_iso(), now_iso() if stuck and not replay else None, now_iso(), now_iso()),
        )
        return cursor.lastrowid
    knowledge_point_id = row[0]
    if replay:
        return knowledge_point_id
    if stuck:
        conn.execute(
            "UPDATE knowledge_points SET box = 0, correct_streak = 0, due_date = NULL, "
            "last_wrong_time = ?, "
            "last_asked_time = ?, updated_time = ? WHERE id = ?",
            (now_iso(), now_iso(), now_iso(), knowledge_point_id),
        )
    else:
        conn.execute(
            "UPDATE knowledge_points SET last_asked_time = ?, updated_time = ? WHERE id = ?",
            (now_iso(), now_iso(), knowledge_point_id),
        )
    return knowledge_point_id


def link_question_knowledge_in_transaction(
    conn,
    user_id: str,
    question_id: int,
    knowledge_point_id: int,
) -> None:
    """Idempotently link a same-user question and concept; reject ownership mismatch."""
    inserted = conn.execute(
        "INSERT INTO question_knowledge (question_id, knowledge_point_id) "
        "SELECT q.id, kp.id FROM questions q CROSS JOIN knowledge_points kp "
        "WHERE q.id = ? AND kp.id = ? AND q.user_id = ? AND kp.user_id = ? "
        "ON CONFLICT(question_id, knowledge_point_id) DO NOTHING RETURNING 1",
        (question_id, knowledge_point_id, user_id, user_id),
    ).fetchone()
    if inserted is not None:
        return
    owned = conn.execute(
        "SELECT 1 FROM questions q CROSS JOIN knowledge_points kp "
        "WHERE q.id = ? AND kp.id = ? AND q.user_id = ? AND kp.user_id = ?",
        (question_id, knowledge_point_id, user_id, user_id),
    ).fetchone()
    if owned is None:
        raise ValueError("question and knowledge point must belong to the same user")
