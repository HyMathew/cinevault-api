"""Metadata authorisation and poster projections over business repository ports."""

from ..metadata import MetadataBusy, MetadataUnavailable, validated_poster
from ..metadata_model import QID, valid_source
from ..validation import ValidationError
from .authentication import authenticated
from .catalogue import canonical_identity
from .results import ServiceResult, failure


def metadata_problem(error):
    busy = isinstance(error, MetadataBusy)
    return failure(
        429 if busy else 503,
        "metadata_unavailable",
        "Le fournisseur externe est temporairement indisponible. Le catalogue reste utilisable.",
        headers={"Retry-After": str(error.retry_after)} if busy else None,
    )


def require_provider(provider):
    if provider is None:
        raise MetadataUnavailable()
    return provider


def verified_detail(provider, identity):
    """Resolve and verify the canonical source used by detail and catalogue enrichment."""
    if not isinstance(identity, str) or not QID.fullmatch(identity):
        raise ValidationError({"external_id": "Identifiant Wikidata invalide."})
    value = require_provider(provider).detail(identity)
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


class MetadataService:
    def __init__(self, persistence, provider):
        self._persistence = persistence
        self._provider = provider

    def _manage(self, token, timestamp, result):
        def authorize(work, user, session):
            if user["role"] not in ("manager", "admin"):
                return failure(403, "forbidden", "Accès interdit.")
            return result

        return authenticated(self._persistence, token, timestamp, authorize)

    def _read(self, token, clock, operation):
        # Preserve session activity at both authorisation points. No provider or
        # cache operation can be replayed by a storage transaction retry.
        gate = self._manage(token, clock(), ServiceResult(204))
        if gate.status != 204:
            return gate
        try:
            value = operation()
        except ValueError:
            raise ValidationError({"query": "Recherche ou identifiant invalide."}) from None
        except MetadataUnavailable as error:
            return metadata_problem(error)
        return self._manage(token, clock(), ServiceResult(200, body=value))

    def search(self, token, parameters, *, clock):
        def operation():
            if len(parameters) != 1 or parameters[0][0] != "q":
                raise ValidationError({"query": "Indiquez une seule recherche."})
            return require_provider(self._provider).search(parameters[0][1])

        return self._read(token, clock, operation)

    def detail(self, token, identity, parameters, *, clock):
        def operation():
            if parameters:
                raise ValidationError({"query": "Paramètres invalides."})
            return verified_detail(self._provider, identity)

        return self._read(token, clock, operation)

    def posters(self, parameters):
        if not 1 <= len(parameters) <= 100 or any(name != "movie_id" for name, _ in parameters):
            raise ValidationError({"movie_id": "Indiquez entre un et cent films."})
        identities = [identity for _, identity in parameters]
        if not all(canonical_identity(identity) for identity in identities):
            raise ValidationError({"movie_id": "Identifiant invalide."})

        def sources(work):
            return [
                (movie["id"], source["qid"], source["revision"])
                for movie in work.catalogue.movies_by_ids(list(dict.fromkeys(identities)))
                if (source := movie.get("external_source")) is not None
            ]

        selected = self._persistence.query(sources)
        result = {}
        if self._provider is not None and selected:
            keys = list(dict.fromkeys("detail:" + qid for _, qid, _ in selected))
            cached_items = self._provider.cache.peek_many(keys)
            for identity, qid, revision in selected:
                cached = cached_items.get("detail:" + qid)
                film = cached.get("film") if isinstance(cached, dict) else None
                if (
                    isinstance(film, dict)
                    and film.get("id") == qid
                    and type(film.get("revision")) is int
                    and film["revision"] == revision
                ):
                    poster = validated_poster(film.get("poster"))
                    if poster is not None:
                        result[identity] = poster
        return ServiceResult(200, body={"posters": result})
