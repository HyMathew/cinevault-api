"""Private collections HTTP boundary; ownership and audit live in shared services."""

from flask import Blueprint, current_app, request, url_for

from .enterprise_auth import bearer, no_cache, now, payload, run_service
from .personal_model import attributes as attributes
from .services.personal import PersonalCollectionService

bp = Blueprint("enterprise_collections", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)


def service():
    return PersonalCollectionService(current_app.extensions["enterprise_persistence"])


@bp.get("/collections")
def list_entries():
    return run_service(
        lambda: service().entries(bearer(), list(request.args.items(multi=True)), timestamp=now())
    )


@bp.post("/collections")
def create_entry():
    gate = run_service(lambda: service().authorize(bearer(), timestamp=now()))
    if gate.status_code != 204:
        return gate
    data = payload(("movie_id", "kind"), ("list_name", "supports"))
    response = run_service(lambda: service().create(bearer(), data, timestamp=now()))
    if response.status_code == 201:
        response.headers["Location"] = url_for(
            "enterprise_collections.get_entry", entry_id=response.get_json()["id"]
        )
    return response


@bp.get("/collections/<entry_id>")
def get_entry(entry_id):
    return run_service(lambda: service().get(bearer(), entry_id, timestamp=now()))


@bp.put("/collections/<entry_id>")
def replace_entry(entry_id):
    gate = run_service(
        lambda: service().authorize(
            bearer(), identity=entry_id, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )
    if gate.status_code != 204:
        return gate
    data = payload(("list_name", "supports"))
    return run_service(
        lambda: service().replace(
            bearer(), entry_id, data, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )


@bp.delete("/collections/<entry_id>")
def delete_entry(entry_id):
    return run_service(
        lambda: service().delete(
            bearer(), entry_id, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )


@bp.get("/audit-events")
def list_events():
    return run_service(
        lambda: service().audit(bearer(), list(request.args.items(multi=True)), timestamp=now())
    )
