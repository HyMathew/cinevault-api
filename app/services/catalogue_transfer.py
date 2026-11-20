"""Import and export over the shared catalogue, session, receipt and audit ports."""

import json
import uuid
from dataclasses import dataclass, field

from ..catalogue_query import movie_identity_key
from ..catalogue_transfer import ImportConflict, export_csv, export_document, normalize, parse_csv
from ..circulation_model import MANAGERS, PayloadError, payload
from ..personal_model import make_event
from ..receipt_storage import digest
from ..validation import ValidationError
from .authentication import authenticated, authenticated_query
from .receipts import RECEIPT_CAPACITY, RECEIPT_TTL, cached_problem, key_digest
from .results import ServiceResult, failure

OPERATION = "catalog.import"
_ABSENT = object()


@dataclass(frozen=True)
class ImportFilm:
    movie: dict
    explicit_id: bool
    copies: list[dict]


@dataclass(frozen=True)
class ImportRequest:
    policy: str
    key_hash: str
    request_hash: str
    films: list[ImportFilm] = field(repr=False)


def _prepare_import(read_body, key):
    """Read and normalize once, after the caller has passed an authority check."""
    try:
        data = payload(read_body(), ("format", "content"), ("duplicate_policy",))
    except (PayloadError, ValidationError) as exc:
        return exc

    encoding, content = data["format"], data["content"]
    policy = data.get("duplicate_policy", "reject")
    try:
        if encoding not in ("json", "csv") or policy not in ("reject", "skip", "append_copies"):
            raise ValidationError({"file": "Format ou politique invalide."})
        try:
            text = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            )
            if len(text.encode("utf-8")) > 1024 * 1024:
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise ValidationError({"file": "Fichier UTF-8 requis, de 1 Mio maximum."}) from None
        incoming = normalize(parse_csv(content) if encoding == "csv" else content)
    except ValidationError as exc:
        return ServiceResult(
            422,
            error={
                "code": "validation_error",
                "message": "Catalogue invalide.",
                "fields": exc.fields,
            },
        )

    if key is None:
        return failure(428, "operation_key_required", "Une clé d’opération est requise.")
    try:
        key_hash = key_digest(key)
    except ValidationError as exc:
        return exc
    # Historical receipts hash the original carrier with the default policy expanded.
    request_hash = digest({"format": encoding, "content": content, "duplicate_policy": policy})
    films = []
    for entry in incoming:
        attributes = {name: value for name, value in entry.items() if name not in ("id", "copies")}
        movie = {"id": entry.get("id") or str(uuid.uuid4()), **attributes}
        copies = [{**copy, "id": copy.get("id") or str(uuid.uuid4())} for copy in entry["copies"]]
        films.append(ImportFilm(movie, "id" in entry, copies))
    return ImportRequest(policy, key_hash, request_hash, films)


def _compare_and_plan(catalogue, prepared):
    movies, copies = [], []
    counts = {"created_movies": 0, "created_copies": 0, "skipped_movies": 0, "skipped_copies": 0}
    keys = [movie_identity_key(film.movie) for film in prepared.films]
    matches = catalogue.import_movie_matches(keys)
    copy_ids = [copy["id"] for film in prepared.films for copy in film.copies]
    occupied = catalogue.occupied_import_ids(
        [film.movie["id"] for film in prepared.films] + copy_ids
    )
    existing_copies = (
        catalogue.import_copies([identity for identity in copy_ids if identity in occupied])
        if prepared.policy == "append_copies"
        else {}
    )
    for film, key in zip(prepared.films, keys, strict=True):
        candidate_id = film.movie["id"]
        existing = matches[key]
        if len(existing) > 1:
            raise ImportConflict()
        if existing:
            current = existing[0]
            movie_id = current["id"]
            if (film.explicit_id and candidate_id in occupied and candidate_id != movie_id) or (
                prepared.policy == "reject"
            ):
                raise ImportConflict()
            counts["skipped_movies"] += 1
            if prepared.policy == "skip":
                counts["skipped_copies"] += len(film.copies)
                continue
            if any(
                current.get(field, _ABSENT) != value
                for field, value in film.movie.items()
                if field != "id"
            ):
                raise ImportConflict()
        else:
            movie_id = candidate_id
            if movie_id in occupied:
                raise ImportConflict()
            movies.append(film.movie)
            occupied.add(movie_id)
        for entry in film.copies:
            copy = {**entry, "movie_id": movie_id}
            if copy["id"] in occupied:
                if existing_copies.get(copy["id"]) == copy:
                    counts["skipped_copies"] += 1
                    continue
                raise ImportConflict()
            copies.append(copy)
            occupied.add(copy["id"])
    counts["created_movies"], counts["created_copies"] = len(movies), len(copies)
    return movies, copies, counts


def _authority(work, user, session):
    if user["role"] not in MANAGERS:
        return failure(403, "forbidden", "Accès interdit.")
    return ServiceResult(204)


class CatalogueTransferService:
    def __init__(self, persistence):
        self._persistence = persistence

    def _manage(self, token, timestamp, operation):
        def command(work, user, session):
            check = _authority(work, user, session)
            return operation(work, user) if check.status == 204 else check

        return authenticated(self._persistence, token, timestamp, command)

    def import_catalogue(self, token, read_body, *, key, timestamp):
        check = authenticated_query(self._persistence, token, timestamp, _authority)
        if check.status != 204:
            if check.status == 403:
                # Preserve the historical activity on a valid but unprivileged session.
                return authenticated(self._persistence, token, timestamp, lambda *_: check)
            return check

        prepared = _prepare_import(read_body, key)
        operation_id, event_id, receipt_id = (str(uuid.uuid4()) for _ in range(3))

        def transfer(work, user):
            def audit(action):
                work.events.append(
                    make_event(event_id, user["id"], "import", operation_id, action, timestamp)
                )

            if isinstance(prepared, (PayloadError, ValidationError)):
                raise prepared from None
            if isinstance(prepared, ServiceResult):
                if prepared.status == 422:
                    audit("failed")
                return prepared

            work.receipts.prune_expired(timestamp)
            cached = work.receipts.get(user["id"], prepared.key_hash)
            if cached is not None:
                problem = cached_problem(cached, OPERATION, prepared.request_hash, timestamp)
                if problem is not None:
                    return problem
                return ServiceResult(
                    200, body=cached["response"], headers={"Idempotency-Replayed": "true"}
                )
            if work.receipts.count() >= RECEIPT_CAPACITY:
                return failure(503, "receipt_capacity", "Capacité d’opérations atteinte.")

            try:
                movies, copies, counts = _compare_and_plan(work.catalogue, prepared)
            except ImportConflict:
                audit("failed")
                return failure(
                    409,
                    "import_conflict",
                    "Conflit dans le lot ; aucun film ou exemplaire importé.",
                )

            work.catalogue.insert_import(movies, copies)
            audit("completed")
            body = {"operation_id": operation_id, **counts}
            work.receipts.insert(
                {
                    "id": receipt_id,
                    "actor_id": user["id"],
                    "key_hash": prepared.key_hash,
                    "request_hash": prepared.request_hash,
                    "operation": OPERATION,
                    "created_at": timestamp,
                    "expires_at": timestamp + RECEIPT_TTL,
                    "response": body,
                }
            )
            return ServiceResult(200, body=body)

        return self._manage(token, timestamp, transfer)

    def export_catalogue(self, token, parameters, *, timestamp):
        parameters = tuple(parameters)
        operation_id, event_id = str(uuid.uuid4()), str(uuid.uuid4())

        def transfer(work, user):
            encoding = parameters[0][1] if parameters else "json"
            if (
                len(parameters) > 1
                or (parameters and parameters[0][0] != "format")
                or encoding not in ("json", "csv")
            ):
                raise ValidationError({"format": "Format invalide."})
            content = export_document(*work.catalogue.export_records())
            result = {
                "format": encoding,
                "content": export_csv(content) if encoding == "csv" else content,
                "filename": "cinevault-catalogue." + encoding,
            }
            work.events.append(
                make_event(event_id, user["id"], "export", operation_id, "completed", timestamp)
            )
            return ServiceResult(200, body=result)

        return self._manage(token, timestamp, transfer)
