"""Private collection commands and minimal audit over common repository ports."""

import copy
import uuid

from ..personal_model import attributes, audit_query, entries_query, make_event
from ..validation import ValidationError
from .authentication import authenticated, authenticated_query
from .catalogue import canonical_identity, missing, version_problem
from .catalogue_model import etag, resource
from .results import ServiceResult, failure


class PersonalCollectionService:
    def __init__(self, persistence):
        self._persistence = persistence

    def _authenticated(self, token, timestamp, operation, *, read_only=False):
        execute = authenticated_query if read_only else authenticated
        return execute(
            self._persistence, token, timestamp, lambda work, user, session: operation(work, user)
        )

    @staticmethod
    def _owned(work, user, identity):
        return (
            work.personal.get_owned(user["id"], identity) if canonical_identity(identity) else None
        )

    def authorize(self, token, *, identity=None, if_match=None, timestamp):
        def check(work, user):
            if identity is not None:
                entry = self._owned(work, user, identity)
                if entry is None:
                    return missing()
                problem = version_problem(entry, if_match)
                if problem is not None:
                    return problem
            return ServiceResult(204)

        return self._authenticated(token, timestamp, check, read_only=True)

    def entries(self, token, parameters, *, timestamp):
        def read(work, user):
            query = entries_query(parameters)
            entries, total = work.personal.page(user["id"], query)
            movies = work.catalogue.movies_by_ids(list({entry["movie_id"] for entry in entries}))
            return ServiceResult(
                200,
                body={
                    "entries": [resource(entry) for entry in entries],
                    "total": total,
                    "limit": query.limit,
                    "offset": query.offset,
                    "movies": {movie["id"]: resource(movie) for movie in movies},
                },
            )

        return self._authenticated(token, timestamp, read)

    def get(self, token, identity, *, timestamp):
        def read(work, user):
            entry = self._owned(work, user, identity)
            return (
                missing()
                if entry is None
                else ServiceResult(200, body=resource(entry), headers={"ETag": etag(entry)})
            )

        return self._authenticated(token, timestamp, read)

    def create(self, token, data, *, timestamp):
        data = copy.deepcopy(data)
        identity, event_id = str(uuid.uuid4()), str(uuid.uuid4())

        def create(work, user):
            if (
                type(data) is not dict
                or not {"movie_id", "kind"}.issubset(data)
                or set(data) - {"movie_id", "kind", "list_name", "supports"}
            ):
                raise ValidationError({"body": "Champs invalides."})
            if not isinstance(data["movie_id"], str):
                raise ValidationError({"movie_id": "Identifiant de film invalide."})
            attrs = attributes(data["kind"], data)
            if (
                not canonical_identity(data["movie_id"])
                or work.catalogue.movie(data["movie_id"]) is None
            ):
                return missing()
            entry = {
                "id": identity,
                "owner_id": user["id"],
                "movie_id": data["movie_id"],
                "kind": data["kind"],
                **attrs,
                "created_at": timestamp,
            }
            if work.personal.has_duplicate(entry):
                return failure(409, "duplicate", "Cette entrée existe déjà dans votre collection.")
            work.personal.insert(entry)
            work.events.append(
                make_event(event_id, user["id"], "collection", identity, "created", timestamp)
            )
            return ServiceResult(201, body=resource(entry), headers={"ETag": etag(entry)})

        return self._authenticated(token, timestamp, create)

    def replace(self, token, identity, data, *, if_match, timestamp):
        data = copy.deepcopy(data)
        event_id = str(uuid.uuid4())

        def replace(work, user):
            entry = self._owned(work, user, identity)
            if entry is None:
                return missing()
            problem = version_problem(entry, if_match)
            if problem is not None:
                return problem
            if type(data) is not dict or set(data) != {"list_name", "supports"}:
                raise ValidationError({"body": "Champs invalides."})
            candidate = {**entry, **attributes(entry["kind"], data)}
            if work.personal.has_duplicate(candidate, excluded_id=identity):
                return failure(409, "duplicate", "Cette entrée existe déjà dans votre collection.")
            work.personal.save_owned(user["id"], candidate)
            work.events.append(
                make_event(event_id, user["id"], "collection", identity, "updated", timestamp)
            )
            return ServiceResult(200, body=resource(candidate), headers={"ETag": etag(candidate)})

        return self._authenticated(token, timestamp, replace)

    def delete(self, token, identity, *, if_match, timestamp):
        event_id = str(uuid.uuid4())

        def delete(work, user):
            entry = self._owned(work, user, identity)
            if entry is None:
                return missing()
            problem = version_problem(entry, if_match)
            if problem is not None:
                return problem
            work.personal.delete_owned(user["id"], identity)
            work.events.append(
                make_event(event_id, user["id"], "collection", identity, "deleted", timestamp)
            )
            return ServiceResult(204)

        return self._authenticated(token, timestamp, delete)

    def audit(self, token, parameters, *, timestamp):
        def read(work, user):
            if user["role"] != "admin":
                return failure(403, "forbidden", "Accès interdit.")
            limit, offset = audit_query(parameters)
            events, total = work.events.page(limit, offset)
            return ServiceResult(
                200, body={"events": events, "total": total, "limit": limit, "offset": offset}
            )

        return self._authenticated(token, timestamp, read)
