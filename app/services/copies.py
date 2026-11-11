"""Copy mutations and their creation receipts over shared persistence ports."""

import copy
import uuid

from ..receipt_storage import digest
from ..validation import ValidationError, is_unicode
from .authentication import authenticated
from .catalogue import canonical_identity, missing, version_problem
from .catalogue_model import COPY_FIELDS, copy_data, etag, resource
from .receipts import RECEIPT_CAPACITY, RECEIPT_TTL, cached_problem, key_digest
from .results import ServiceResult, failure


class CopyService:
    def __init__(self, persistence):
        self._persistence = persistence

    def _manage(self, token, timestamp, operation):
        def authorized(work, user, session):
            if user["role"] not in ("manager", "admin"):
                return failure(403, "forbidden", "Accès interdit.")
            return operation(work, user)

        return authenticated(self._persistence, token, timestamp, authorized)

    @staticmethod
    def _current(work, identity, if_match):
        if not canonical_identity(identity):
            return None, missing()
        entity = work.catalogue.copy(identity)
        if entity is None:
            return None, missing()
        return entity, version_problem(entity, if_match)

    def authorize(self, token, *, identity=None, if_match=None, timestamp):
        def authorize(work, user):
            if identity is not None:
                _, problem = self._current(work, identity, if_match)
                if problem is not None:
                    return problem
            return ServiceResult(204)

        return self._manage(token, timestamp, authorize)

    def create(self, token, data, *, key=None, timestamp):
        data = copy.deepcopy(data)
        copy_id, receipt_id = str(uuid.uuid4()), str(uuid.uuid4())

        def create(work, user):
            if type(data) is not dict or set(data) != {"movie_id", *COPY_FIELDS}:
                raise ValidationError({"body": "Champs invalides."})
            movie_id = data["movie_id"]
            if not isinstance(movie_id, str) or not is_unicode(movie_id):
                raise ValidationError({"movie_id": "Identifiant de film invalide."})
            attributes = copy_data({name: data[name] for name in COPY_FIELDS})
            key_hash = key_digest(key)
            request_hash = digest(data) if key_hash is not None else None
            if key_hash is not None:
                work.receipts.prune_expired(timestamp)
                cached = work.receipts.get(user["id"], key_hash)
                if cached is not None:
                    problem = cached_problem(cached, "copy.create", request_hash, timestamp)
                    if problem is not None:
                        return problem
                    return ServiceResult(
                        201,
                        body=cached["response"],
                        headers={
                            "ETag": cached["response"]["_etag"],
                            "Idempotency-Replayed": "true",
                        },
                    )
                if work.receipts.count() >= RECEIPT_CAPACITY:
                    return failure(503, "receipt_capacity", "Capacité d’opérations atteinte.")
            if not canonical_identity(movie_id) or work.catalogue.movie(movie_id) is None:
                return missing()
            entity = {"id": copy_id, "movie_id": movie_id, **attributes}
            work.catalogue.insert_copy(entity)
            body = resource(entity)
            if key_hash is not None:
                work.receipts.insert(
                    {
                        "id": receipt_id,
                        "actor_id": user["id"],
                        "key_hash": key_hash,
                        "request_hash": request_hash,
                        "operation": "copy.create",
                        "created_at": timestamp,
                        "expires_at": timestamp + RECEIPT_TTL,
                        "response": body,
                    }
                )
            return ServiceResult(201, body=body, headers={"ETag": etag(entity)})

        return self._manage(token, timestamp, create)

    def replace(self, token, identity, attributes, *, if_match, timestamp):
        attributes = copy.deepcopy(attributes)

        def replace(work, user):
            entity, problem = self._current(work, identity, if_match)
            if problem is not None:
                return problem
            entity.update(copy_data(attributes))
            work.catalogue.save_copy(entity)
            return ServiceResult(200, body=resource(entity), headers={"ETag": etag(entity)})

        return self._manage(token, timestamp, replace)

    def delete(self, token, identity, *, if_match, timestamp):
        def delete(work, user):
            entity, problem = self._current(work, identity, if_match)
            if problem is not None:
                return problem
            if work.catalogue.has_copy_history(identity):
                return failure(409, "referenced", "Un historique de prêt référence cet exemplaire.")
            work.catalogue.delete_copy(identity)
            return ServiceResult(204)

        return self._manage(token, timestamp, delete)
