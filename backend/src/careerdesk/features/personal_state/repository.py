"""SQLite repository for the feature-private personal-state read model."""

from ...platform.database import loads_json, read_connection


class PersonalStateRepository:
    def __init__(self, db_path: str):
        self._db_path = db_path

    def recent(self, user_id: str, limit: int = 10) -> list[dict]:
        """Read the user's status records in reverse date/write order."""
        with read_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT log_date, time_of_day, mood, factors_json FROM status_log "
                "WHERE user_id = ? ORDER BY log_date DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            {
                "log_date": log_date,
                "time_of_day": time_of_day,
                "mood": mood,
                "factors": loads_json(factors_json, []),
            }
            for log_date, time_of_day, mood, factors_json in rows
        ]
