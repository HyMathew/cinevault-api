"""Catalogue validation and representation, independent of HTTP and persistence."""

import hashlib
import json

from ..metadata_model import valid_source
from ..validation import ValidationError, is_unicode, validate

COPY_FIELDS = ("support", "location", "condition")
SUPPORTS = ("numerique", "dvd", "blu-ray", "vhs", "autre")


def etag(entity):
    encoded = json.dumps(entity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return '"' + hashlib.sha256(encoded).hexdigest() + '"'


def resource(entity):
    return {**entity, "_etag": etag(entity)}


def movie_data(data):
    legacy = validate(
        {
            "titre": data["title"],
            "annee": data["year"],
            "realisateur": data["director"],
            "acteurs": data["actors"],
            "synopsis": data["synopsis"],
            "support": "autre",
        }
    )
    genres = data.get("genres", [])
    if (
        not isinstance(genres, list)
        or len(genres) > 30
        or any(
            not isinstance(g, str) or not is_unicode(g) or not 1 <= len(g) <= 80 or g.strip() != g
            for g in genres
        )
    ):
        raise ValidationError({"genres": "Liste de genres invalide."})
    if len({g.casefold() for g in genres}) != len(genres):
        raise ValidationError({"genres": "Les genres doivent être distincts."})
    result = {
        "title": legacy["titre"],
        "year": legacy["annee"],
        "director": legacy["realisateur"],
        "actors": legacy["acteurs"],
        "synopsis": legacy["synopsis"],
        "genres": genres,
    }
    if "external_source" in data:
        if not valid_source(data["external_source"]):
            raise ValidationError({"external_source": "Provenance invalide."})
        result["external_source"] = data["external_source"]
    return result


def copy_data(data):
    if not isinstance(data, dict) or set(data) != set(COPY_FIELDS):
        raise ValidationError({"copy": "Champs de l’exemplaire invalides."})
    if (
        data["support"] not in SUPPORTS
        or data["condition"] not in ("unknown", "good", "damaged")
        or not isinstance(data["location"], str)
        or not is_unicode(data["location"])
        or len(data["location"]) > 200
    ):
        raise ValidationError({"copy": "Attributs de l’exemplaire invalides."})
    return dict(data)
