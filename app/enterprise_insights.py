from flask import Blueprint, request

from .enterprise_auth import forbidden, no_cache, now, protected
from .enterprise_catalogue import bounded_integer
from .insights import recommendations, statistics
from .validation import ValidationError, is_unicode

bp = Blueprint("enterprise_insights", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)


@bp.get("/statistics")
def get_statistics():
    def calculate(document, user, session):
        if (
            set(request.args) - {"scope"}
            or len(request.args.getlist("scope")) > 1
            or request.args.get("scope", "mine") not in ("mine", "all")
        ):
            raise ValidationError({"scope": "Portée invalide."})
        all_operations = request.args.get("scope", "mine") == "all"
        if all_operations and user["role"] not in ("manager", "admin"):
            return forbidden()
        return statistics(document, user["id"], now(), all_operations)

    return protected(calculate)


@bp.get("/recommendations")
def get_recommendations():
    def calculate(document, user, session):
        genres = request.args.getlist("genre")
        if (
            set(request.args) - {"genre", "limit"}
            or len(request.args.getlist("limit")) > 1
            or len(genres) > 10
            or any(
                not is_unicode(genre) or not 1 <= len(genre) <= 80 or genre != genre.strip()
                for genre in genres
            )
        ):
            raise ValidationError({"query": "Préférences ou limites invalides."})
        limit = bounded_integer("limit", 12, 1, 24)
        return recommendations(document, user["id"], now(), genres, limit)

    return protected(calculate)
