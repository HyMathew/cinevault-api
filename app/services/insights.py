"""Authorization, parameter rules and one current snapshot for each insight."""

from ..validation import ValidationError, is_unicode
from .authentication import authenticated
from .results import ServiceResult, failure


class InsightsService:
    def __init__(self, persistence):
        self._persistence = persistence

    def statistics(self, token, parameters, *, timestamp):
        def calculate(work, actor, session):
            if len(parameters) > 1 or any(
                key != "scope" or value not in ("mine", "all") for key, value in parameters
            ):
                raise ValidationError({"scope": "Portée invalide."})
            all_operations = parameters == (("scope", "all"),)
            if all_operations and actor["role"] not in ("manager", "admin"):
                return failure(403, "forbidden", "Accès interdit.")
            return ServiceResult(
                200, body=work.insights.statistics(actor["id"], timestamp, all_operations)
            )

        parameters = tuple(parameters)
        return authenticated(self._persistence, token, timestamp, calculate)

    def recommendations(self, token, parameters, *, timestamp):
        def calculate(work, actor, session):
            genres = tuple(value for key, value in parameters if key == "genre")
            limits = tuple(value for key, value in parameters if key == "limit")
            if (
                len(limits) > 1
                or any(key not in ("genre", "limit") for key, _ in parameters)
                or len(genres) > 10
                or any(
                    not is_unicode(genre) or not 1 <= len(genre) <= 80 or genre != genre.strip()
                    for genre in genres
                )
            ):
                raise ValidationError({"query": "Préférences ou limites invalides."})
            try:
                limit = int(limits[0]) if limits else 12
                if not 1 <= limit <= 24:
                    raise ValueError()
            except ValueError as exc:
                raise ValidationError({"limit": "Valeur invalide."}) from exc
            return ServiceResult(
                200, body=work.insights.recommendations(actor["id"], timestamp, genres, limit)
            )

        parameters = tuple(parameters)
        return authenticated(self._persistence, token, timestamp, calculate)
