"""Loan HTTP boundary; shared circulation policy owns its transaction."""

from flask import Blueprint, current_app, request, url_for

from .enterprise_auth import bearer, capture_body, no_cache, now, run_service
from .services.circulation import CirculationService

bp = Blueprint("enterprise_lending", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)


def service():
    return CirculationService(current_app.extensions["enterprise_persistence"])


@bp.post("/loans")
def request_loan():
    timestamp = now()
    body = capture_body()
    response = run_service(
        lambda: service().request_loan(
            bearer(),
            body,
            key=request.headers.get("Idempotency-Key"),
            timestamp=timestamp,
        )
    )
    if response.status_code == 201:
        response.headers["Location"] = url_for(
            "enterprise_lending.get_loan", loan_id=response.get_json()["id"]
        )
    return response


@bp.get("/loans")
def list_loans():
    return run_service(
        lambda: service().loans(
            bearer(),
            list(request.args.items(multi=True)),
            timestamp=now(),
        )
    )


@bp.get("/loans/<loan_id>")
def get_loan(loan_id):
    return run_service(lambda: service().loan(bearer(), loan_id, timestamp=now()))


@bp.post("/loans/<loan_id>/approve")
def approve_loan(loan_id):
    timestamp = now()
    body = capture_body()
    return run_service(
        lambda: service().approve(
            bearer(),
            loan_id,
            body,
            if_match=request.headers.get("If-Match"),
            timestamp=timestamp,
        )
    )


@bp.get("/loans/<loan_id>/options")
def loan_options(loan_id):
    return run_service(lambda: service().options(bearer(), loan_id, timestamp=now()))


@bp.post("/loans/<loan_id>/return")
def return_loan(loan_id):
    timestamp, body = now(), capture_body()
    return run_service(
        lambda: service().return_loan(
            bearer(),
            loan_id,
            body=body,
            if_match=request.headers.get("If-Match"),
            timestamp=timestamp,
        )
    )


@bp.post("/loans/<loan_id>/cancel")
def cancel_loan(loan_id):
    timestamp, body = now(), capture_body()
    return run_service(
        lambda: service().cancel_loan(
            bearer(),
            loan_id,
            body=body,
            if_match=request.headers.get("If-Match"),
            timestamp=timestamp,
        )
    )


@bp.get("/availability")
def availability():
    return run_service(
        lambda: service().availability(list(request.args.items(multi=True)), timestamp=now())
    )
