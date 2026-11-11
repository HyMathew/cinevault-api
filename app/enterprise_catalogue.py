"""Catalogue resources; entity versions prevent silent stale replacements."""

from flask import Blueprint, current_app, jsonify, request, url_for

from .enterprise_auth import (
    bearer,
    error,
    forbidden,
    no_cache,
    now,
    payload,
    protected,
    run_service,
)
from .services.catalogue import CatalogueService
from .services.catalogue_model import COPY_FIELDS, copy_data, etag, movie_data, resource
from .services.catalogue_model import SUPPORTS as SUPPORTS
from .services.copies import CopyService
from .services.public_catalogue import PublicCatalogueService
from .validation import ValidationError

bp = Blueprint("enterprise_catalogue", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)
MOVIE_FIELDS = ("title", "year", "director", "actors", "synopsis")


def respond(entity, status=200, location=None):
    response = jsonify(resource(entity))
    response.status_code = status
    response.headers["ETag"] = etag(entity)
    if location:
        response.headers["Location"] = location
    return response


def missing():
    return error(404, "not_found", "Ressource introuvable.")


def lookup(document, collection, identity):
    return next((item for item in document[collection] if item["id"] == identity), None)


def management(operation):
    def action(document, user, session):
        if user["role"] not in ("manager", "admin"):
            return forbidden()
        return operation(document, user)

    return protected(action)


def precondition(entity):
    supplied = request.headers.get("If-Match")
    if not supplied:
        return error(428, "version_required", "La version de la ressource est requise.")
    if supplied != etag(entity):
        return error(
            412, "stale_version", "Cette ressource a changé. Rechargez-la avant de réessayer."
        )
    return None


def catalogue_service():
    return CatalogueService(current_app.extensions["enterprise_persistence"])


def copy_service():
    return CopyService(current_app.extensions["enterprise_persistence"])


def public_catalogue_service():
    return PublicCatalogueService(current_app.extensions["enterprise_persistence"])


def prepare_movie(optional, movie_id=None):
    gate = run_service(
        lambda: catalogue_service().authorize(
            bearer(), identity=movie_id, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )
    if gate.status_code != 204:
        return gate
    data = payload(MOVIE_FIELDS, optional)
    attributes = movie_data(data)
    initial = copy_data(data["initial_copy"]) if "initial_copy" in data else None
    if "external_id" in data:
        if data["external_id"] is None:
            attributes["external_source"] = None
        else:
            from .enterprise_metadata import load_detail, metadata_failure
            from .metadata import MetadataUnavailable

            try:
                film = load_detail(data["external_id"])["film"]
            except MetadataUnavailable as exc:
                return metadata_failure(exc)
            attributes["external_source"] = {
                "qid": film["id"],
                "revision": film["revision"],
                "retrieved_at": film["retrieved_at"],
            }
    return {"attributes": attributes, "initial_copy": initial}


def bounded_integer(name, default, lower, upper):
    try:
        value = int(request.args.get(name, default))
        if not lower <= value <= upper:
            raise ValueError()
        return value
    except ValueError:
        raise ValidationError({name: "Valeur invalide."})


@bp.get("/movies")
def list_movies():
    return run_service(
        lambda: public_catalogue_service().movies(list(request.args.items(multi=True)))
    )


@bp.get("/movies/<movie_id>")
def get_movie(movie_id):
    return run_service(lambda: public_catalogue_service().movie(movie_id))


@bp.post("/movies")
def create_movie():
    prepared = prepare_movie(("genres", "initial_copy", "external_id"))
    if not isinstance(prepared, dict):
        return prepared
    response = run_service(
        lambda: catalogue_service().create(
            bearer(), prepared["attributes"], initial_copy=prepared["initial_copy"], timestamp=now()
        )
    )
    if response.status_code == 201:
        response.headers["Location"] = url_for(
            "enterprise_catalogue.get_movie", movie_id=response.get_json()["id"]
        )
    return response


@bp.put("/movies/<movie_id>")
def replace_movie(movie_id):
    prepared = prepare_movie(("genres", "external_id"), movie_id)
    if not isinstance(prepared, dict):
        return prepared
    return run_service(
        lambda: catalogue_service().replace(
            bearer(),
            movie_id,
            prepared["attributes"],
            if_match=request.headers.get("If-Match"),
            timestamp=now(),
        )
    )


@bp.delete("/movies/<movie_id>")
def delete_movie(movie_id):
    return run_service(
        lambda: catalogue_service().delete(
            bearer(), movie_id, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )


@bp.get("/copies")
def list_copies():
    return run_service(
        lambda: public_catalogue_service().copies(list(request.args.items(multi=True)))
    )


@bp.get("/copies/<copy_id>")
def get_copy(copy_id):
    return run_service(lambda: public_catalogue_service().copy(copy_id))


@bp.post("/copies")
def create_copy():
    gate = run_service(lambda: copy_service().authorize(bearer(), timestamp=now()))
    if gate.status_code != 204:
        return gate
    data = payload(("movie_id", *COPY_FIELDS))
    response = run_service(
        lambda: copy_service().create(
            bearer(), data, key=request.headers.get("Idempotency-Key"), timestamp=now()
        )
    )
    if response.status_code == 201:
        response.headers["Location"] = url_for(
            "enterprise_catalogue.get_copy", copy_id=response.get_json()["id"]
        )
    return response


@bp.put("/copies/<copy_id>")
def replace_copy(copy_id):
    gate = run_service(
        lambda: copy_service().authorize(
            bearer(), identity=copy_id, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )
    if gate.status_code != 204:
        return gate
    data = payload(COPY_FIELDS)
    return run_service(
        lambda: copy_service().replace(
            bearer(), copy_id, data, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )


@bp.delete("/copies/<copy_id>")
def delete_copy(copy_id):
    return run_service(
        lambda: copy_service().delete(
            bearer(), copy_id, if_match=request.headers.get("If-Match"), timestamp=now()
        )
    )
