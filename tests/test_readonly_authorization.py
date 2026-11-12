"""Preflight authorization reads authority without committing session activity."""

import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store

NOW = 1000
MOVIE = {
    "title": "Préflight",
    "year": 2026,
    "director": "Auteur",
    "actors": [],
    "genres": [],
    "synopsis": "",
}
COPY = {"support": "dvd", "condition": "good", "location": "A"}


def fixtures(backend):
    token = uuid.uuid4().hex * 2
    user = {
        "id": str(uuid.uuid4()),
        "username": "preflight_" + uuid.uuid4().hex[:12],
        "display_name": "Admin",
        "password_hash": "$argon2id$fixture",
        "role": "admin",
        "enabled": True,
        "created_at": 0,
    }
    session = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "created_at": NOW,
        "last_seen": NOW,
        "expires_at": NOW + 28800,
    }
    movie = {"id": str(uuid.uuid4()), **MOVIE, "title": MOVIE["title"] + uuid.uuid4().hex[:8]}
    item = {"id": str(uuid.uuid4()), "movie_id": movie["id"], **COPY}
    entry = {
        "id": str(uuid.uuid4()),
        "owner_id": user["id"],
        "movie_id": movie["id"],
        "kind": "custom",
        "list_name": "Avant",
        "supports": [],
        "created_at": NOW,
    }
    backend.run(
        lambda work: (
            work.accounts.insert(user),
            work.sessions.insert(session),
            work.catalogue.insert_movie(movie),
            work.catalogue.insert_copy(item),
            work.personal.insert(entry),
        )
    )
    return token, user, session, movie, item, entry


def exercise_readonly_authority(backend, *, observer=None):
    from app.services.authentication import authenticated_query
    from app.services.catalogue import CatalogueService
    from app.services.catalogue_model import etag
    from app.services.copies import CopyService
    from app.services.personal import PersonalCollectionService

    token, _, session, movie, item, entry = fixtures(backend)
    before = backend.query(lambda work: work.revision)
    if observer is not None:
        observer.clear()
    for service, entity in (
        (CatalogueService(backend), movie),
        (CopyService(backend), item),
        (PersonalCollectionService(backend), entry),
    ):
        assert service.authorize(token, timestamp=NOW + 1).status == 204
        assert (
            service.authorize(
                token, identity=entity["id"], if_match=etag(entity), timestamp=NOW + 1
            ).status
            == 204
        )
        assert (
            service.authorize(token, identity=entity["id"], if_match=None, timestamp=NOW + 1).status
            == 428
        )
        assert (
            service.authorize(
                token, identity=entity["id"], if_match='"stale"', timestamp=NOW + 1
            ).status
            == 412
        )
    assert backend.query(lambda work: work.revision) == before
    assert backend.query(lambda work: work.sessions.by_token_hash(session["token_hash"])) == session
    try:
        authenticated_query(
            backend,
            token,
            NOW + 1,
            lambda work, user, current: work.sessions.touch(current["id"], NOW + 1),
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("Read-only authorization allowed a session write")
    if observer is not None:
        assert observer
        assert all(
            command in {"find", "aggregate"}
            and collection
            in {"coordination", "users", "sessions", "movies", "copies", "collection_entries"}
            for command, collection in observer
        )
    observed = len(observer) if observer is not None else 0
    changed = CopyService(backend).replace(
        token, item["id"], {**COPY, "location": "B"}, if_match=etag(item), timestamp=NOW + 1
    )
    assert changed.status == 200
    assert backend.query(lambda work: work.revision) == before + 1
    assert (
        backend.query(lambda work: work.sessions.by_token_hash(session["token_hash"]))["last_seen"]
        == NOW + 1
    )
    return {
        "read_only_authorization": "passed",
        "preflight_revision_and_session_unchanged": True,
        "final_write_revision_delta": 1,
        "native_read_trace": {"checked": observer is not None, "commands": observed},
    }


def exercise_readonly_authorization(database, *, observer=None):
    from app.persistence.mongo import MongoPersistence

    return exercise_readonly_authority(MongoPersistence(database), observer=observer)


class ReadOnlyPreflightHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.json"
        V2Store(self.path).initialize()
        self.backend = JsonPersistence(self.path)
        self.token, self.user, self.session, self.movie, self.item, self.entry = fixtures(
            self.backend
        )
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_FILE": str(Path(self.temp.name) / "legacy.json"),
                "ENTERPRISE_ENABLED": True,
                "ENTERPRISE_DATA_FILE": str(self.path),
                "AUTH_THROTTLE_KEY": b"x" * 32,
                "AUTH_CLOCK": lambda: NOW + 1,
            }
        )
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Bearer " + self.token}

    def test_bad_json_and_invalid_fields_do_not_write_revision_or_last_seen(self):
        cases = (
            ("/api/v1/movies", {**MOVIE, "title": ""}),
            ("/api/v1/copies", {"movie_id": self.movie["id"], **COPY, "condition": "invalid"}),
            (
                "/api/v1/collections",
                {"movie_id": self.movie["id"], "kind": "custom", "list_name": ""},
            ),
        )
        for endpoint, body in cases:
            for malformed in (True, False):
                with self.subTest(endpoint=endpoint, malformed=malformed):
                    before = self.path.read_bytes()
                    snapshot = V2Store(self.path).read()
                    kwargs = (
                        {"data": "{", "content_type": "application/json"}
                        if malformed
                        else {"json": body}
                    )
                    response = self.client.post(endpoint, headers=self.auth, **kwargs)
                    self.assertEqual(response.status_code, 400 if malformed else 422)
                    self.assertEqual(self.path.read_bytes(), before)
                    current = V2Store(self.path).read()
                    self.assertEqual(current["revision"], snapshot["revision"])
                    self.assertEqual(current["sessions"], snapshot["sessions"])

    def test_same_readonly_policy_and_final_write_contract_on_real_json(self):
        self.assertEqual(
            exercise_readonly_authority(self.backend)["read_only_authorization"], "passed"
        )

    def test_precondition_failure_precedes_parsing_without_prolonging_session(self):
        for kind, identity in (
            ("movies", self.movie["id"]),
            ("copies", self.item["id"]),
            ("collections", self.entry["id"]),
        ):
            for supplied, expected in ((None, 428), ('"stale"', 412)):
                with self.subTest(kind=kind, supplied=supplied):
                    before = self.path.read_bytes()
                    headers = self.auth if supplied is None else {**self.auth, "If-Match": supplied}
                    response = self.client.put(
                        "/api/v1/" + kind + "/" + identity,
                        headers=headers,
                        data="{",
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, expected)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_success_commits_one_command_and_updates_last_seen_in_that_command(self):
        for endpoint, body in (
            ("/api/v1/movies", {**MOVIE, "title": "Nouveau" + uuid.uuid4().hex[:8]}),
            ("/api/v1/copies", {"movie_id": self.movie["id"], **COPY}),
            ("/api/v1/collections", {"movie_id": self.movie["id"], "kind": "favorite"}),
        ):
            with self.subTest(endpoint=endpoint):
                before = V2Store(self.path).read()
                persistence = self.app.extensions["enterprise_persistence"]
                with patch.object(persistence, "run", wraps=persistence.run) as writes:
                    response = self.client.post(endpoint, headers=self.auth, json=body)
                self.assertEqual(response.status_code, 201)
                after = V2Store(self.path).read()
                self.assertEqual(after["revision"], before["revision"] + 1)
                self.assertEqual(after["sessions"][0]["last_seen"], NOW + 1)
                self.assertEqual(writes.call_count, 1)

    def test_revocation_during_external_metadata_is_rechecked_without_creation(self):
        from app import enterprise_metadata

        revoked = []

        def detail(identity):
            self.backend.run(lambda work: work.sessions.revoke_user(self.user["id"]))
            revoked.append(self.path.read_bytes())
            return {"film": {"id": identity, "revision": 1, "retrieved_at": NOW}}

        with patch.object(enterprise_metadata, "load_detail", side_effect=detail):
            response = self.client.post(
                "/api/v1/movies", headers=self.auth, json={**MOVIE, "external_id": "Q1"}
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.path.read_bytes(), revoked[0])
        self.assertEqual(len(V2Store(self.path).read()["movies"]), 1)
