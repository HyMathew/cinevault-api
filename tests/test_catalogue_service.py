"""Same movie command contract on a real file and an explicitly provided Mongo DB."""

import hashlib
import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store

MOVIE = {
    "title": "Échos, lumières",
    "year": 2021,
    "director": "Une réalisatrice",
    "actors": ["Nom, composé"],
    "synopsis": "Une histoire.",
    "genres": ["Drame"],
}
COPY = {"support": "dvd", "location": "Étagère A", "condition": "good"}
NOW = 1000


def service_type():
    assert importlib.util.find_spec("app.services.catalogue") is not None, (
        "A pure catalogue command service is required"
    )
    from app.services.catalogue import CatalogueService

    return CatalogueService


def identities(backend):
    tokens = {}
    for role in ("manager", "member"):
        identity, session_id = str(uuid.uuid4()), str(uuid.uuid4())
        token = uuid.uuid4().hex + uuid.uuid4().hex
        account = {
            "id": identity,
            "username": "catalogue_" + uuid.uuid4().hex[:12],
            "display_name": "Lecteur",
            "password_hash": "$argon2id$fixture",
            "role": role,
            "enabled": True,
            "created_at": 0,
        }
        session = {
            "id": session_id,
            "user_id": identity,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": NOW,
            "last_seen": NOW,
            "expires_at": NOW + 28800,
        }

        def insert(work):
            work.accounts.insert(account)
            work.sessions.insert(session)

        backend.run(insert)
        tokens[role] = token
    return tokens


def exercise_movie_commands(backend):
    service = service_type()(backend)
    tokens = identities(backend)
    token = tokens["manager"]
    before = backend.query(lambda work: work.revision)
    assert service.create("invalid", MOVIE, timestamp=NOW).status == 401
    assert backend.query(lambda work: work.revision) == before
    assert service.create(tokens["member"], MOVIE, timestamp=NOW).status == 403
    created = service.create(token, MOVIE, initial_copy=COPY, timestamp=NOW)
    assert created.status == 201 and created.body["actors"] == MOVIE["actors"]
    movie_id, version = created.body["id"], created.headers["ETag"]
    copies = backend.query(lambda work: work.catalogue.copies_for_movie(movie_id))
    assert len(copies) == 1 and copies[0]["location"] == COPY["location"]
    assert (
        service.create(token, {**MOVIE, "title": MOVIE["title"].upper()}, timestamp=NOW).status
        == 409
    )
    changed = {**MOVIE, "synopsis": "Un autre texte."}
    assert service.replace(token, movie_id, changed, if_match=None, timestamp=NOW).status == 428
    assert (
        service.replace(token, movie_id, changed, if_match='"stale"', timestamp=NOW).status == 412
    )
    replaced = service.replace(token, movie_id, changed, if_match=version, timestamp=NOW)
    assert replaced.status == 200 and replaced.headers["ETag"] != version
    assert service.delete(token, movie_id, if_match=version, timestamp=NOW).status == 412
    assert (
        service.delete(token, movie_id, if_match=replaced.headers["ETag"], timestamp=NOW).status
        == 409
    )
    empty = service.create(token, {**MOVIE, "title": "Sans copie"}, timestamp=NOW)
    assert empty.status == 201
    assert (
        service.delete(
            token, empty.body["id"], if_match=empty.headers["ETag"], timestamp=NOW
        ).status
        == 204
    )
    assert backend.query(lambda work: work.catalogue.movie(empty.body["id"])) is None
    assert (
        backend.query(lambda work: work.catalogue.movie(movie_id))["synopsis"]
        == changed["synopsis"]
    )
    return {
        "movie_commands": "passed",
        "atomic_initial_copy": True,
        "etag_and_reference_checks": True,
    }


class CatalogueServiceTests(unittest.TestCase):
    def test_shared_command_contract_on_real_json_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalogue.json"
            V2Store(path).initialize()
            self.assertEqual(
                exercise_movie_commands(JsonPersistence(path))["movie_commands"], "passed"
            )

    def test_replayed_create_keeps_ids_and_aborted_initial_copy_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalogue.json"
            V2Store(path).initialize()
            backend = JsonPersistence(path)
            token = identities(backend)["manager"]
            seen = []

            class AbortAttempt(Exception):
                pass

            class Replay:
                def run(self, command):
                    def apply(work, abort):
                        result = command(work)
                        movie_id = result.body["id"]
                        copy_id = work.catalogue.copies_for_movie(movie_id)[0]["id"]
                        seen.append((movie_id, copy_id))
                        if abort:
                            raise AbortAttempt()
                        return result

                    try:
                        backend.run(lambda work: apply(work, True))
                    except AbortAttempt:
                        pass
                    return backend.run(lambda work: apply(work, False))

            result = service_type()(Replay()).create(token, MOVIE, initial_copy=COPY, timestamp=NOW)
            self.assertEqual(result.status, 201)
            self.assertEqual(seen[0], seen[1])
            document = V2Store(path).read()
            self.assertEqual(len(document["movies"]), 1)
            self.assertEqual(len(document["copies"]), 1)


def exercise_catalogue_service(database):
    """Caller supplies a new, provisioned Mongo database; no mongomock substitution."""
    from app.persistence.mongo import MongoPersistence

    return exercise_movie_commands(MongoPersistence(database))


if __name__ == "__main__":
    unittest.main()
