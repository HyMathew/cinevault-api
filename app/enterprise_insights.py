from flask import Blueprint, current_app, request

from .enterprise_auth import bearer, no_cache, now, run_service
from .services.insights import InsightsService

bp = Blueprint("enterprise_insights", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)


def insights_service():
    return InsightsService(current_app.extensions["enterprise_persistence"])


@bp.get("/statistics")
def get_statistics():
    return run_service(
        lambda: insights_service().statistics(
            bearer(), tuple(request.args.items(multi=True)), timestamp=now()
        )
    )


@bp.get("/recommendations")
def get_recommendations():
    return run_service(
        lambda: insights_service().recommendations(
            bearer(), tuple(request.args.items(multi=True)), timestamp=now()
        )
    )
