"""Insights adapter over the already validated JSON snapshot."""

from ..insights import recommendations, statistics


class JsonInsights:
    def __init__(self, scope):
        self._scope = scope

    def _rows(self, names):
        return {name: self._scope.rows(name) for name in names}

    def statistics(self, actor: str, timestamp: int, all_operations: bool) -> dict:
        data = self._rows(
            ("movies", "copies", "loans", "reservations", "collections", "audit_events")
        )
        return statistics(data, actor, timestamp, all_operations)

    def recommendations(
        self, actor: str, timestamp: int, genres: tuple[str, ...], limit: int
    ) -> dict:
        data = self._rows(("movies", "copies", "loans", "reservations", "collections"))
        return recommendations(data, actor, timestamp, genres, limit)
