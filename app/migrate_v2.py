"""Explicit offline migration: legacy source is never modified."""

import hashlib
import hmac
import json
import uuid
from pathlib import Path

import portalocker

from .repository import unique_pairs
from .storage_v2 import (
    _encode,
    _valid_uuid,
    _write_new,
    decode_document,
    empty_document,
    validate_document,
)
from .validation import ValidationError, validate

COPY_NAMESPACE = uuid.UUID("a2b5289d-6345-46a0-96d9-e11f30b27412")
LEGACY_FIELDS = {"id", "titre", "annee", "realisateur", "acteurs", "support", "synopsis"}


def create_candidate(source, candidate):
    source, candidate = Path(source), Path(candidate)
    with portalocker.Lock(str(source) + ".lock", mode="a", timeout=10):
        raw = source.read_bytes()
        try:
            legacy = json.loads(
                raw,
                object_pairs_hook=unique_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid constant")),
            )
            if (
                not isinstance(legacy, dict)
                or set(legacy) != {"films"}
                or not isinstance(legacy["films"], list)
            ):
                raise ValueError("Invalid legacy document")
            document = empty_document()
            seen = set()
            for film in legacy["films"]:
                if (
                    not isinstance(film, dict)
                    or set(film) != LEGACY_FIELDS
                    or not _valid_uuid(film["id"])
                    or film["id"] in seen
                ):
                    raise ValueError("Invalid legacy film or duplicate id")
                seen.add(film["id"])
                fields = {k: v for k, v in film.items() if k != "id"}
                if validate(fields) != fields:
                    raise ValueError("Non-canonical legacy film")
                document["movies"].append(
                    {
                        "id": film["id"],
                        "title": film["titre"],
                        "year": film["annee"],
                        "director": film["realisateur"],
                        "actors": film["acteurs"],
                        "synopsis": film["synopsis"],
                        "genres": [],
                    }
                )
                document["copies"].append(
                    {
                        "id": str(uuid.uuid5(COPY_NAMESPACE, film["id"])),
                        "movie_id": film["id"],
                        "support": film["support"],
                        "location": "",
                        "condition": "unknown",
                    }
                )
            validate_document(document)
            encoded = _encode(document)
        except (ValidationError, UnicodeDecodeError, RecursionError, TypeError) as exc:
            raise ValueError("Invalid legacy document") from exc
        _write_new(candidate, encoded)
        return {
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "candidate_sha256": hashlib.sha256(encoded).hexdigest(),
            "movies": len(document["movies"]),
            "copies": len(document["copies"]),
        }


def promote_candidate(
    candidate, destination, *, source, expected_source_sha256, expected_candidate_sha256
):
    source, candidate, destination = Path(source), Path(candidate), Path(destination)
    if len({source.resolve(), candidate.resolve(), destination.resolve()}) != 3:
        raise ValueError("Source, candidate and destination must differ")
    # The legacy service must stay stopped during and after the cutover.
    with portalocker.Lock(str(source) + ".lock", mode="a", timeout=10):
        with portalocker.Lock(str(destination) + ".lock", mode="a", timeout=10):
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            raw = candidate.read_bytes()
            candidate_hash = hashlib.sha256(raw).hexdigest()
            if not hmac.compare_digest(
                source_hash, expected_source_sha256
            ) or not hmac.compare_digest(candidate_hash, expected_candidate_sha256):
                raise ValueError("Source or candidate changed since validation")
            decode_document(raw)
            _write_new(destination, raw)
            return candidate_hash
