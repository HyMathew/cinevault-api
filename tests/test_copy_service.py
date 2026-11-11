"""Copy/receipt command contract, using real JSON or an explicit native database."""

import hashlib
import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store

NOW = 1000
KEY = "copy-operation-synthetic-123"
ATTRIBUTES = {"support": "dvd", "location": "Étagère A", "condition": "good"}


def service_type():
    assert importlib.util.find_spec("app.services.copies") is not None, "Pure copy service required"
    from app.services.copies import CopyService

    return CopyService


def seed(backend):
    actors = {}
    for role in ("manager", "member"):
        identity, token = str(uuid.uuid4()), uuid.uuid4().hex * 2
        user = {
            "id": identity,
            "username": "copies_" + uuid.uuid4().hex[:12],
            "display_name": "Membre",
            "password_hash": "$argon2id$fixture",
            "role": role,
            "enabled": True,
            "created_at": 0,
        }
        session = {
            "id": str(uuid.uuid4()),
            "user_id": identity,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": NOW,
            "last_seen": NOW,
            "expires_at": NOW + 28800,
        }
        backend.run(lambda work: (work.accounts.insert(user), work.sessions.insert(session)))
        actors[role] = (identity, token)
    movie = {
        "id": str(uuid.uuid4()),
        "title": "Échos",
        "year": 2026,
        "director": "Auteur",
        "actors": [],
        "synopsis": "",
        "genres": [],
    }
    backend.run(lambda work: work.catalogue.insert_movie(movie))
    return actors, movie


def exercise_copy_commands(backend):
    service = service_type()(backend)
    actors, movie = seed(backend)
    actor, token = actors["manager"]
    data = {"movie_id": movie["id"], **ATTRIBUTES}
    before = backend.query(lambda work: work.revision)
    assert service.create("invalid", data, key=KEY, timestamp=NOW).status == 401
    assert backend.query(lambda work: work.revision) == before
    assert service.create(actors["member"][1], data, key=KEY, timestamp=NOW).status == 403
    first = service.create(token, data, key=KEY, timestamp=NOW)
    replay = service.create(token, data, key=KEY, timestamp=NOW)
    assert first.status == replay.status == 201 and first.body == replay.body
    assert first.headers["ETag"] == replay.headers["ETag"] == first.body["_etag"]
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(backend.query(lambda work: work.catalogue.copies_for_movie(movie["id"]))) == 1
    assert service.create(token, {**data, "location": "B"}, key=KEY, timestamp=NOW).status == 409
    assert backend.query(lambda work: work.receipts.count()) == 1
    version, identity = first.body["_etag"], first.body["id"]
    assert service.replace(token, identity, ATTRIBUTES, if_match=None, timestamp=NOW).status == 428
    assert service.replace(token, identity, {}, if_match='"stale"', timestamp=NOW).status == 412
    changed = service.replace(
        token, identity, {**ATTRIBUTES, "location": "B"}, if_match=version, timestamp=NOW
    )
    assert changed.status == 200 and changed.headers["ETag"] != version
    assert service.delete(token, identity, if_match=version, timestamp=NOW).status == 412
    assert (
        service.delete(token, identity, if_match=changed.headers["ETag"], timestamp=NOW).status
        == 204
    )
    backend.run(lambda work: work.catalogue.delete_movie(movie["id"]))
    replay = service.create(token, data, key=KEY, timestamp=NOW)
    assert replay.status == 201 and replay.body == first.body
    for identity in ("not-uuid", str(uuid.uuid4())):
        assert service.delete(token, identity, if_match=None, timestamp=NOW).status == 404
    key_hash = hashlib.sha256(KEY.encode()).hexdigest()
    receipt = backend.query(lambda work: work.receipts.get(actor, key_hash))
    assert receipt["response"] == first.body
    receipt["response"]["location"] = "detached"
    assert backend.query(lambda work: work.receipts.get(actor, key_hash))["response"] == first.body
    assert backend.run(lambda work: work.receipts.prune_expired(NOW + 86400)) == 1
    assert backend.query(lambda work: work.receipts.count()) == 0
    return {"copy_service": "passed", "replay_after_deletion": True, "expiry_and_detachment": True}


class CopyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.json"
        V2Store(self.path).initialize()
        self.backend = JsonPersistence(self.path)

    def test_shared_copy_and_receipt_contract_on_real_json(self):
        self.assertEqual(exercise_copy_commands(self.backend)["copy_service"], "passed")

    def test_future_receipt_and_cross_operation_conflict_precede_movie_lookup(self):
        from app.receipt_storage import digest

        service = service_type()(self.backend)
        actors, movie = seed(self.backend)
        actor, token = actors["manager"]
        key_hash = hashlib.sha256(KEY.encode()).hexdigest()
        record = {
            "id": str(uuid.uuid4()),
            "actor_id": actor,
            "key_hash": key_hash,
            "request_hash": digest({}),
            "operation": "catalog.import",
            "created_at": NOW + 1,
            "expires_at": NOW + 86400,
            "response": {
                "operation_id": str(uuid.uuid4()),
                "created_movies": 0,
                "created_copies": 0,
                "skipped_movies": 0,
                "skipped_copies": 0,
            },
        }
        self.backend.run(lambda work: work.receipts.insert(record))
        result = service.create(
            token, {"movie_id": movie["id"], **ATTRIBUTES}, key=KEY, timestamp=NOW
        )
        self.assertEqual(result.error["code"], "clock_state")
        result = service.create(
            token, {"movie_id": "missing", **ATTRIBUTES}, key=KEY, timestamp=NOW + 1
        )
        self.assertEqual(result.error["code"], "operation_conflict")

    def test_retry_prepares_copy_and_receipt_uuid_once_and_commits_one_pair(self):
        service_type()
        actors, movie = seed(self.backend)
        actor, token = actors["manager"]
        observed, backend = [], self.backend

        class Abort(Exception):
            pass

        class Retry:
            def run(self, callback):
                def invoke(work, abort):
                    result = callback(work)
                    receipt = work.receipts.get(actor, hashlib.sha256(KEY.encode()).hexdigest())
                    observed.append((result.body["id"], receipt["id"]))
                    if abort:
                        raise Abort()
                    return result

                try:
                    backend.run(lambda work: invoke(work, True))
                except Abort:
                    pass
                return backend.run(lambda work: invoke(work, False))

        result = service_type()(Retry()).create(
            token, {"movie_id": movie["id"], **ATTRIBUTES}, key=KEY, timestamp=NOW
        )
        self.assertEqual(result.status, 201)
        self.assertEqual(observed[0], observed[1])
        document = V2Store(self.path).read()
        self.assertEqual((len(document["copies"]), len(document["idempotency_records"])), (1, 1))

    def test_receipt_selector_mutation_and_readonly_guards(self):
        actors, _ = seed(self.backend)
        actor = actors["manager"][0]
        for wrong_actor, wrong_key in (({"$ne": None}, "a" * 64), (actor, {"$ne": None})):
            with self.assertRaises(ValueError):
                self.backend.query(lambda work: work.receipts.get(wrong_actor, wrong_key))
        with self.assertRaises(RuntimeError):
            self.backend.query(lambda work: work.receipts.prune_expired(NOW))

    def test_capacity_blocks_new_key_but_optional_key_and_replay_remain_available(self):
        service = service_type()(self.backend)
        actors, movie = seed(self.backend)
        actor, token = actors["manager"]
        data = {"movie_id": movie["id"], **ATTRIBUTES}
        first = service.create(token, data, key=KEY, timestamp=NOW)
        records = [_import_receipt(actor, key_hash=f"{index:064x}") for index in range(9999)]
        V2Store(self.path).transact(
            lambda document: document["idempotency_records"].extend(records)
        )
        self.assertEqual(
            service.create(token, data, key="another-synthetic-key", timestamp=NOW).status, 503
        )
        self.assertEqual(service.create(token, data, key=KEY, timestamp=NOW).body, first.body)
        self.assertEqual(service.create(token, data, timestamp=NOW).status, 201)
        document = V2Store(self.path).read()
        self.assertEqual(
            (len(document["copies"]), len(document["idempotency_records"])), (2, 10000)
        )

    def test_invalid_receipt_aborts_the_whole_command(self):
        actors, _ = seed(self.backend)
        actor = actors["manager"][0]
        for change in (
            {"actor_id": str(uuid.uuid4())},
            {"key_hash": "invalid"},
            {"expires_at": NOW},
            {"response": {}},
        ):
            receipt = {**_import_receipt(actor), **change}
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                self.backend.run(lambda work: work.receipts.insert(receipt))
            self.assertEqual(self.path.read_bytes(), before)

    def test_expired_receipt_purge_crosses_a_500_record_batch_boundary(self):
        actors, _ = seed(self.backend)
        receipts = [
            _import_receipt(actors["manager"][0], key_hash=f"{index:064x}") for index in range(501)
        ]
        V2Store(self.path).transact(
            lambda document: document["idempotency_records"].extend(receipts)
        )
        self.assertEqual(
            self.backend.run(lambda work: work.receipts.prune_expired(NOW + 86400)), 501
        )
        self.assertEqual(self.backend.query(lambda work: work.receipts.count()), 0)

    def test_historical_copy_cannot_be_deleted(self):
        def insert(loan):
            V2Store(self.path).transact(lambda document: document["loans"].append(loan))

        self.assertTrue(exercise_copy_history(self.backend, insert)["copy_history_preserved"])


def _import_receipt(actor, *, key_hash=None):
    from app.receipt_storage import digest

    return {
        "id": str(uuid.uuid4()),
        "actor_id": actor,
        "key_hash": key_hash or hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        "request_hash": digest({}),
        "operation": "catalog.import",
        "created_at": NOW,
        "expires_at": NOW + 86400,
        "response": {
            "operation_id": str(uuid.uuid4()),
            "created_movies": 0,
            "created_copies": 0,
            "skipped_movies": 0,
            "skipped_copies": 0,
        },
    }


def exercise_copy_history(backend, insert_fixture):
    service = service_type()(backend)
    actors, movie = seed(backend)
    actor, token = actors["manager"]
    created = service.create(token, {"movie_id": movie["id"], **ATTRIBUTES}, timestamp=NOW)
    loan = {
        "id": str(uuid.uuid4()),
        "movie_id": movie["id"],
        "borrower_id": actor,
        "copy_id": created.body["id"],
        "status": "returned",
        "requested_at": NOW,
        "approved_at": NOW,
        "due_at": NOW + 100,
        "returned_at": NOW + 1,
        "cancelled_at": None,
    }
    insert_fixture(loan)
    refused = service.delete(
        token, created.body["id"], if_match=created.headers["ETag"], timestamp=NOW
    )
    assert refused.status == 409 and refused.error["code"] == "referenced"
    assert backend.query(lambda work: work.catalogue.copy(created.body["id"])) is not None
    return {"copy_history_preserved": True}


def test_copy_http_service_boundary_and_original_authorization_parse_order(tmp_path, monkeypatch):
    from test_enterprise_auth import account
    from test_enterprise_catalogue import MOVIE

    from app import create_app
    from app.services.results import ServiceResult

    path = tmp_path / "enterprise.json"
    V2Store(path).initialize()
    client = create_app(
        {
            "TESTING": True,
            "DATA_FILE": str(tmp_path / "legacy.json"),
            "ENTERPRISE_ENABLED": True,
            "ENTERPRISE_DATA_FILE": str(path),
            "AUTH_THROTTLE_KEY": b"x" * 32,
        }
    ).test_client()
    _, manager = account(client, "copy_boundary_manager", "manager", path)
    _, member = account(client, "copy_boundary_member")
    movie = client.post("/api/v1/movies", headers=manager, json=MOVIE).json
    persistence = client.application.extensions["enterprise_persistence"]
    run = persistence.run

    def observed(command):
        def pure(work):
            result = command(work)
            assert isinstance(result, ServiceResult)
            return result

        return run(pure)

    monkeypatch.setattr(persistence, "run", observed)
    monkeypatch.setattr(
        client.application.extensions["enterprise_store"],
        "transact",
        lambda _: (_ for _ in ()).throw(AssertionError("Legacy mutation used")),
    )
    assert client.post("/api/v1/copies", data="invalid").status_code == 401
    assert client.post("/api/v1/copies", headers=member, data="invalid").status_code == 403
    first = client.post(
        "/api/v1/copies",
        headers={**manager, "Idempotency-Key": KEY},
        json={"movie_id": movie["id"], **ATTRIBUTES},
    )
    assert first.status_code == 201
    endpoint = first.headers["Location"]
    assert client.put(endpoint, headers=manager, data="invalid").status_code == 428
    assert (
        client.put(endpoint, headers={**manager, "If-Match": '"stale"'}, data="invalid").status_code
        == 412
    )
    assert client.put("/api/v1/copies/not-uuid", headers=manager, data="invalid").status_code == 404
    assert client.delete("/api/v1/copies/not-uuid", headers=member).status_code == 403
    assert client.delete("/api/v1/copies/not-uuid", headers=manager).status_code == 404
    updated = client.put(
        endpoint,
        headers={**manager, "If-Match": first.headers["ETag"]},
        json={**ATTRIBUTES, "location": "B"},
    )
    assert updated.status_code == 200
    assert (
        client.delete(
            endpoint, headers={**manager, "If-Match": updated.headers["ETag"]}
        ).status_code
        == 204
    )


def exercise_copy_service(database):
    from app.persistence.mongo import MongoPersistence
    from app.persistence.mongo_model import encode_record

    backend = MongoPersistence(database)
    result = exercise_copy_commands(backend)

    def insert(loan):
        database.loans.insert_one(encode_record("loans", loan))
        database.entity_ids.insert_one({"_id": loan["id"], "kind": "loans"})

    result.update(exercise_copy_history(backend, insert))
    result.update(exercise_native_receipt_boundaries(database, backend))
    return result


def exercise_native_receipt_boundaries(database, backend):
    """Real cursor/deletion batches and FK rollback; fixture setup uses bulk writes."""
    from pymongo import ReadPreference
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern

    from app.persistence.mongo_model import encode_record

    actor = database.users.find_one({}, {"_id": 1})["_id"]
    rows = [_import_receipt(actor, key_hash=f"{index:064x}") for index in range(501)]
    with database.client.start_session() as session:
        with session.start_transaction(
            read_concern=ReadConcern("snapshot"),
            read_preference=ReadPreference.PRIMARY,
            write_concern=WriteConcern("majority", wtimeout=5000),
        ):
            database.idempotency_records.insert_many(
                [encode_record("idempotency_records", row) for row in rows], session=session
            )
            database.entity_ids.insert_many(
                [{"_id": row["id"], "kind": "idempotency_records"} for row in rows],
                session=session,
            )
    assert backend.run(lambda work: work.receipts.prune_expired(NOW + 86400)) == 501
    assert backend.query(lambda work: work.receipts.count()) == 0
    assert database.entity_ids.count_documents({"kind": "idempotency_records"}) == 0
    for record in (_import_receipt(str(uuid.uuid4())), {**_import_receipt(actor), "response": {}}):
        before = backend.query(lambda work: work.revision)
        try:
            backend.run(lambda work: work.receipts.insert(record))
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid native receipt was accepted")
        assert backend.query(lambda work: work.revision) == before
        assert database.idempotency_records.find_one({"_id": record["id"]}) is None
        assert database.entity_ids.find_one({"_id": record["id"]}) is None
    return {"native_expired_receipts_removed": 501, "invalid_native_receipt_commands_aborted": 2}
