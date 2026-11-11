"""Public catalogue projections and validation shared by JSON and MongoDB."""

from ..catalogue_query import parse_movie_query, query_values
from .catalogue import canonical_identity, missing
from .catalogue_model import etag, resource
from .results import ServiceResult


class PublicCatalogueService:
    def __init__(self, persistence):
        self._persistence = persistence

    def movies(self, parameters):
        query = parse_movie_query(parameters)

        def read(work):
            movies, total = work.catalogue.search_movies(query)
            summaries = work.catalogue.copy_summaries([movie["id"] for movie in movies])
            return ServiceResult(
                200,
                body={
                    "movies": [resource(movie) for movie in movies],
                    "total": total,
                    "limit": query.limit,
                    "offset": query.offset,
                    "copy_summaries": summaries,
                },
            )

        return self._persistence.query(read)

    def _entity(self, kind, identity):
        def read(work):
            if not canonical_identity(identity):
                return missing()
            entity = getattr(work.catalogue, kind)(identity)
            return (
                missing()
                if entity is None
                else ServiceResult(200, body=resource(entity), headers={"ETag": etag(entity)})
            )

        return self._persistence.query(read)

    def movie(self, identity):
        return self._entity("movie", identity)

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
