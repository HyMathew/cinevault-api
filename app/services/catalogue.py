"""Movie commands over repository ports; no HTTP responses or driver operations."""

import copy
import uuid

from .authentication import authenticated, authenticated_query
from .catalogue_model import copy_data, etag, movie_data, resource
from .results import ServiceResult, failure


def missing():
    return failure(404, "not_found", "Ressource introuvable.")


def version_problem(entity, supplied):
    if not supplied:
        return failure(428, "version_required", "La version de la ressource est requise.")
    if supplied != etag(entity):
        return failure(
            412, "stale_version", "Cette ressource a changé. Rechargez-la avant de réessayer."
        )
    return None


def canonical_identity(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class CatalogueService:
    def __init__(self, persistence):
        self._persistence = persistence

    def _manage(self, token, timestamp, operation, *, read_only=False):
        def authorized(work, user, session):
            if user["role"] not in ("manager", "admin"):
                return failure(403, "forbidden", "Accès interdit.")
            return operation(work)

        execute = authenticated_query if read_only else authenticated
        return execute(self._persistence, token, timestamp, authorized)

    def authorize(self, token, *, identity=None, if_match=None, timestamp):
        """Check before external metadata I/O; the final command checks again."""

        def authorize(work):
            if identity is not None:
                if not canonical_identity(identity):
                    return missing()
                movie = work.catalogue.movie(identity)
                if movie is None:
                    return missing()
                problem = version_problem(movie, if_match)
                if problem is not None:
                    return problem
            return ServiceResult(204)

        return self._manage(token, timestamp, authorize, read_only=True)

    def create(self, token, attributes, *, initial_copy=None, timestamp):
        attributes = copy.deepcopy(movie_data(attributes))
        copy_attributes = copy_data(initial_copy) if initial_copy is not None else None
        movie_id, copy_id = str(uuid.uuid4()), str(uuid.uuid4())

        def create(work):
            if work.catalogue.has_duplicate_movie(attributes):
                return failure(409, "duplicate", "Ce film existe déjà.")
            movie = {"id": movie_id, **attributes}
            work.catalogue.insert_movie(movie)
            if copy_attributes is not None:
                work.catalogue.insert_copy({"id": copy_id, "movie_id": movie_id, **copy_attributes})
            return ServiceResult(201, body=resource(movie), headers={"ETag": etag(movie)})

        return self._manage(token, timestamp, create)

    def replace(self, token, identity, attributes, *, if_match, timestamp):
        attributes = copy.deepcopy(movie_data(attributes))

        def replace(work):
            if not canonical_identity(identity):
                return missing()
            movie = work.catalogue.movie(identity)
            if movie is None:
                return missing()
            problem = version_problem(movie, if_match)
            if problem is not None:
                return problem
            if work.catalogue.has_duplicate_movie(attributes, excluded_id=identity):
                return failure(409, "duplicate", "Ce film existe déjà.")
            movie.update(attributes)
            work.catalogue.save_movie(movie)
            return ServiceResult(200, body=resource(movie), headers={"ETag": etag(movie)})

        return self._manage(token, timestamp, replace)

    def delete(self, token, identity, *, if_match, timestamp):
        def delete(work):
            if not canonical_identity(identity):
                return missing()
            movie = work.catalogue.movie(identity)
            if movie is None:
                return missing()
            problem = version_problem(movie, if_match)
            if problem is not None:
                return problem
            if work.catalogue.has_movie_references(identity):
                return failure(
                    409, "referenced", "Des exemplaires ou collections référencent ce film."
                )
            work.catalogue.delete_movie(identity)
            return ServiceResult(204)

        return self._manage(token, timestamp, delete)
