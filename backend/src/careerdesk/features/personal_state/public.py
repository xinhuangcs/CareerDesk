"""Stable query entry point for personal state."""

from collections import Counter

from .repository import PersonalStateRepository


class PersonalStateQueries:
    """Read-only use cases shared by HTTP, tools, and cross-feature composition."""

    def __init__(self, repository: PersonalStateRepository):
        self._repository = repository

    def recent(self, user_id: str, limit: int = 10) -> list[dict]:
        return self._repository.recent(user_id, limit)

    def recurring_factors(self, user_id: str, limit: int = 3) -> list[tuple[str, int]]:
        """Return factors seen at least twice in the latest ten records by frequency."""
        counter: Counter = Counter()
        for item in self._repository.recent(user_id, limit=10):
            counter.update(item["factors"])
        return [
            (factor, count)
            for factor, count in counter.most_common(limit)
            if count >= 2
        ]


def build_personal_state_queries(db_path: str) -> PersonalStateQueries:
    return PersonalStateQueries(PersonalStateRepository(db_path))
