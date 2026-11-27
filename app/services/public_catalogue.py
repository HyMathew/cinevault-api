"""Public catalogue projections and validation shared by JSON and MongoDB."""

from ..catalogue_query import parse_movie_query, query_values
from .catalogue import canonical_identity, missing
from .catalogue_model import etag, resource
from .results import ServiceResult


def related_summary(movie, candidate):
    labels = {genre.casefold(): genre for genre in movie["genres"]}
    other_genres = {genre.casefold() for genre in candidate["genres"]}
    return {
        **{key: candidate[key] for key in ("id", "title", "year", "director")},
        "shared_genres": [label for key, label in labels.items() if key in other_genres],
        "same_director": movie["director"].casefold() == candidate["director"].casefold(),
    }


class PublicCatalogueService:
    def __init__(self, persistence):
        self._persistence = persistence

    def movies(self, parameters, *, timestamp=None):
        query, include_availability = parse_movie_query(parameters)

        def read(work):
            movies, total = work.catalogue.search_movies(query)
            identities = [movie["id"] for movie in movies]
            body = {
                "movies": [resource(movie) for movie in movies],
                "total": total,
                "limit": query.limit,
                "offset": query.offset,
                "copy_summaries": work.catalogue.copy_summaries(identities),
            }
            if include_availability:
                body["availability"] = work.circulation.available_by_movie(identities, timestamp)
            return ServiceResult(200, body=body)

        return self._persistence.query(read)

    def _entity(self, kind, identity, *, include_related=False):
        def read(work):
            if not canonical_identity(identity):
                return missing()
            entity = getattr(work.catalogue, kind)(identity)
            if entity is None:
                return missing()
            body = resource(entity)
            transport_version = body["_etag"]
            if include_related:
                body["related"] = [
                    related_summary(entity, other)
                    for other in work.catalogue.related_movies(entity)
                ]
                # The optional representation changes when another film is edited.
                transport_version = etag(body)
            return ServiceResult(200, body=body, headers={"ETag": transport_version})

        return self._persistence.query(read)

    def movie(self, identity, *, include_related=False):
        return self._entity("movie", identity, include_related=include_related)

    def copy(self, identity):
        return self._entity("copy", identity)

    def copies(self, parameters):
        values = query_values(parameters, {"movie_id"})
        movie_id = values.get("movie_id")

        def read(work):
            records = (
                []
                if movie_id is not None and not canonical_identity(movie_id)
                else work.catalogue.copies(movie_id)
            )
            return ServiceResult(200, body={"copies": [resource(record) for record in records]})

        return self._persistence.query(read)
