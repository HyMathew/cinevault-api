"""HTTP boundary for transfers through the shared catalogue service."""

from flask import Blueprint, current_app, request

from .circulation_model import RequestBody
from .enterprise_auth import bearer, capture_body, no_cache, now, run_service
from .services.catalogue_transfer import CatalogueTransferService

bp = Blueprint("enterprise_transfer", __name__, url_prefix="/api/v1/catalogue")
bp.after_request(no_cache)


@bp.before_request
def transfer_limit():
    if request.endpoint == "enterprise_transfer.import_catalogue":
        # A JSON carrier can expand a one-MiB UTF-8 file with escapes.
        request.max_content_length = 8 * 1024 * 1024


def _service():
    return CatalogueTransferService(current_app.extensions["enterprise_persistence"])


def _body():
    return capture_body() if request.is_json else RequestBody(b"", is_json=False)


@bp.post("/import")
def import_catalogue():
    return run_service(
        lambda: _service().import_catalogue(
            bearer(), _body, key=request.headers.get("Idempotency-Key"), timestamp=now()
        )
    )


@bp.get("/export")
def export_catalogue():
    return run_service(
        lambda: _service().export_catalogue(
            bearer(), request.args.items(multi=True), timestamp=now()
        )
    )
