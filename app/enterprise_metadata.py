"""Metadata I/O happens between two short business-authorisation transactions."""

import os
from pathlib import Path

from flask import Blueprint, current_app, request

from .enterprise_auth import bearer, no_cache, now, run_service
from .persistence.factory import file_and_lock, protected_file_paths
from .services.metadata import MetadataService, metadata_problem, verified_detail
from .wikimedia import WikimediaClient

bp = Blueprint("enterprise_metadata", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)


def metadata_failure(exc):
    return run_service(lambda: metadata_problem(exc))


def load_detail(identity):
    return verified_detail(current_app.extensions.get("metadata_client"), identity)


def metadata_service():
    return MetadataService(
        current_app.extensions["enterprise_persistence"],
        current_app.extensions.get("metadata_client"),
    )


@bp.get("/metadata/search")
def search():
    return run_service(
        lambda: metadata_service().search(bearer(), list(request.args.items(multi=True)), clock=now)
    )


@bp.get("/metadata/<identity>")
def detail(identity):
    return run_service(
        lambda: metadata_service().detail(
            bearer(), identity, list(request.args.items(multi=True)), clock=now
        )
    )


@bp.get("/posters")
def posters():
    return run_service(lambda: metadata_service().posters(list(request.args.items(multi=True))))


def install_metadata(app):
    app.extensions["metadata_client"] = None
    enabled = app.config.get(
        "METADATA_ENABLED", os.environ.get("METADATA_ENABLED", "").lower() == "true"
    )
    if enabled is True:
        path = app.config.get("METADATA_CACHE_FILE") or os.environ.get("METADATA_CACHE_FILE")
        if not path:
            business_path = app.config.get("ENTERPRISE_DATA_FILE")
            if not business_path:
                raise RuntimeError("MongoDB metadata requires a cache file")
            path = Path(business_path).with_name("metadata-cache.json")
        admission = app.extensions["registration_admission"]
        reserved = protected_file_paths(app.config) | file_and_lock(admission.path)
        if file_and_lock(path) & reserved:
            raise RuntimeError("Metadata cache must be separate from business files and locks")
        agent = app.config.get("WIKIMEDIA_USER_AGENT") or os.environ.get("WIKIMEDIA_USER_AGENT")
        try:
            app.extensions["metadata_client"] = WikimediaClient(path, agent)
        except ValueError as exc:
            raise RuntimeError(
                "Configure an explicit Wikimedia application User-Agent with contact URL"
            ) from exc
    app.register_blueprint(bp)
