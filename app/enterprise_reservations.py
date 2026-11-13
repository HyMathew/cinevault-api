"""Reservation HTTP boundary; maintenance and mutation share one owned UoW."""

from flask import Blueprint, current_app, request, url_for

from .enterprise_auth import bearer, capture_body, no_cache, now, run_service
from .services.circulation import CirculationService

bp = Blueprint("enterprise_reservations", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)


def service():
    return CirculationService(current_app.extensions["enterprise_persistence"])


@bp.post("/reservations")
def create_reservation():
    timestamp = now()
    body = capture_body()
    response = run_service(
        lambda: service().create_reservation(
            bearer(),
            body,
            key=request.headers.get("Idempotency-Key"),
            timestamp=timestamp,
        )
    )
    if response.status_code == 201:
        response.headers["Location"] = url_for(
            "enterprise_reservations.get_reservation",
            reservation_id=response.get_json()["id"],
        )
    return response


@bp.get("/reservations")
def list_reservations():
    return run_service(
        lambda: service().reservations(
            bearer(),
            list(request.args.items(multi=True)),
            timestamp=now(),
        )
    )


@bp.get("/reservations/<reservation_id>")
def get_reservation(reservation_id):
    return run_service(lambda: service().reservation(bearer(), reservation_id, timestamp=now()))


@bp.post("/reservations/<reservation_id>/cancel")
def cancel_reservation(reservation_id):
    timestamp, body = now(), capture_body()
    return run_service(
        lambda: service().cancel_reservation(
            bearer(),
            reservation_id,
            body=body,
            if_match=request.headers.get("If-Match"),
            timestamp=timestamp,
        )
    )
