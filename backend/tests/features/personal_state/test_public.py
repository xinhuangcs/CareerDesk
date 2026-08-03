
from careerdesk.agentic.tools import QueryStatusTool
from careerdesk.platform.database import init_db, now_iso, transaction
from careerdesk.features.personal_state.public import (
    PersonalStateQueries,
    build_personal_state_queries,
)


def _seed(tmp_path) -> tuple[str, PersonalStateQueries]:
    db_path = str(tmp_path / "careerdesk.db")
    init_db(db_path)
    now = now_iso()
    with transaction(db_path) as conn:
        rows = [
            ("u1", "2026-07-10", "morning", "一般", '["睡眠差", "紧张"]'),
            ("u1", "2026-07-11", "afternoon", "还行", '["睡眠差"]'),
            ("u1", "2026-07-11", "evening", "不错", '["咖啡过量"]'),
            ("u2", "2026-07-12", "morning", "差", '["睡眠差", "睡眠差"]'),
        ]
        conn.executemany(
            "INSERT INTO status_log "
            "(user_id, log_date, time_of_day, mood, factors_json, created_time) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(*row, now) for row in rows],
        )
    return db_path, build_personal_state_queries(db_path)


def test_recent_is_ordered_limited_and_user_isolated(tmp_path):
    _, queries = _seed(tmp_path)

    recent = queries.recent("u1", limit=2)
    assert [item["time_of_day"] for item in recent] == ["evening", "afternoon"]
    assert all(item["mood"] != "差" for item in recent)


def test_recurring_factors_require_two_occurrences(tmp_path):
    _, queries = _seed(tmp_path)

    assert queries.recurring_factors("u1") == [("睡眠差", 2)]
    assert queries.recurring_factors("u2") == [("睡眠差", 2)]


class _FakeQueries:
    def recurring_factors(self, user_id: str):
        assert user_id == "u1"
        return [("紧张", 3)]

    def recent(self, user_id: str):
        assert user_id == "u1"
        return [{
            "log_date": "2026-07-12",
            "time_of_day": "morning",
            "mood": "一般",
            "factors": ["紧张"],
        }]


def test_query_status_tool_only_adapts_public_queries():
    tool = QueryStatusTool(_FakeQueries(), "u1")

    patterns = tool.run({"action": "patterns"})
    assert patterns.status == "success" and "紧张" in patterns.text and "3 次" in patterns.text
    recent = tool.run({"action": "recent"})
    assert recent.status == "success" and "2026-07-12" in recent.text
    assert tool.run({"action": "unknown"}).status == "error"
