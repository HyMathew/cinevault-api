"""Metadata I/O happens between two short business-authorisation transactions."""

import os
import uuid
from pathlib import Path

from flask import Blueprint, current_app, request

from .enterprise_auth import check_store, error, no_cache
from .enterprise_catalogue import management
from .metadata import MetadataBusy, MetadataUnavailable, validated_poster
from .metadata_model import QID, valid_source
from .validation import ValidationError
from .wikimedia import WikimediaClient

bp = Blueprint("enterprise_metadata", __name__, url_prefix="/api/v1")
bp.after_request(no_cache)


def metadata_failure(exc):
    response, status = error(
        429 if isinstance(exc, MetadataBusy) else 503,
        "metadata_unavailable",
        "Le fournisseur externe est temporairement indisponible. Le catalogue reste utilisable.",
    )
    if isinstance(exc, MetadataBusy):
        response.headers["Retry-After"] = str(exc.retry_after)
    return response, status


def provider():
    value = current_app.extensions.get("metadata_client")
    if value is None:
        raise MetadataUnavailable()
    return value


def load_detail(identity):
    if not isinstance(identity, str) or not QID.fullmatch(identity):
        raise ValidationError({"external_id": "Identifiant Wikidata invalide."})
    value = provider().detail(identity)
    film = value.get("film") if isinstance(value, dict) else None
    if (
        not isinstance(film, dict)
        or film.get("id") != identity
        or not valid_source(
            {
                "qid": film.get("id"),
                "revision": film.get("revision"),
                "retrieved_at": film.get("retrieved_at"),
            }
        )
    ):
        raise MetadataUnavailable()
    return value


def authorised_read(operation):
    gate = management(lambda document, user: {"allowed": True})
    if not isinstance(gate, dict):
        return gate
    try:
        result = operation()
    except ValueError:
        raise ValidationError({"query": "Recherche ou identifiant invalide."}) from None
    except MetadataUnavailable as exc:
        return metadata_failure(exc)
    # A role/session can change while waiting for Wikimedia; check it again.
    return management(lambda document, user: result)


@bp.get("/metadata/search")
def search():
    def operation():
        if set(request.args) != {"q"} or len(request.args.getlist("q")) != 1:
            raise ValidationError({"query": "Indiquez une seule recherche."})
        return provider().search(request.args["q"])

    return authorised_read(operation)


@bp.get("/metadata/<identity>")
def detail(identity):
    def operation():
        if request.args:
            raise ValidationError({"query": "Paramètres invalides."})
        return load_detail(identity)

    return authorised_read(operation)


@bp.get("/posters")
def posters():
    identities = request.args.getlist("movie_id")
    if set(request.args) - {"movie_id"} or not 1 <= len(identities) <= 100:
        raise ValidationError({"movie_id": "Indiquez entre un et cent films."})
    try:
        if any(str(uuid.UUID(identity)) != identity for identity in identities):
            raise ValueError()
    except ValueError:
        raise ValidationError({"movie_id": "Identifiant invalide."}) from None
    document = check_store()
    client = current_app.extensions.get("metadata_client")
    result = {}
    if client is not None:
        movies = [
            movie
            for movie in document["movies"]
            if movie["id"] in identities and movie.get("external_source") is not None
        ]
        cached_items = client.cache.peek_many(
            ["detail:" + movie["external_source"]["qid"] for movie in movies]
        )
        for movie in movies:
            source = movie.get("external_source")
            cached = cached_items.get("detail:" + source["qid"])
            film = cached.get("film") if isinstance(cached, dict) else None
            if (
                isinstance(film, dict)
                and film.get("id") == source["qid"]
                and film.get("revision") == source["revision"]
                and isinstance(film.get("poster"), dict)
            ):
                checked = validated_poster(film["poster"])
                if checked is not None:
                    result[movie["id"]] = checked
    return {"posters": result}


def install_metadata(app):
    app.extensions["metadata_client"] = None
    enabled = app.config.get(
        "METADATA_ENABLED", os.environ.get("METADATA_ENABLED", "").lower() == "true"
    )
    if enabled is True:
        path = (
            app.config.get("METADATA_CACHE_FILE")
            or os.environ.get("METADATA_CACHE_FILE")
            or str(Path(app.config["ENTERPRISE_DATA_FILE"]).with_name("metadata-cache.json"))
        )
        reserved = {
            Path(str(value) + suffix).resolve()
            for value in (app.config["ENTERPRISE_DATA_FILE"], app.config["DATA_FILE"])
            for suffix in ("", ".lock")
        }
        if any(Path(str(path) + suffix).resolve() in reserved for suffix in ("", ".lock")):
            raise RuntimeError("Metadata cache must be separate from business files and locks")
        agent = app.config.get("WIKIMEDIA_USER_AGENT") or os.environ.get("WIKIMEDIA_USER_AGENT")
        try:
            app.extensions["metadata_client"] = WikimediaClient(path, agent)
        except ValueError as exc:
            raise RuntimeError(
                "Configure an explicit Wikimedia application User-Agent with contact URL"
            ) from exc
    app.register_blueprint(bp)
