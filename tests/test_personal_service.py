"""Private collections and append-order audit contracts shared by real engines."""

import hashlib
import importlib.util
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store

NOW = 1000


def implementation():
    assert importlib.util.find_spec("app.services.personal") is not None, (
        "Pure personal service required"
    )
    from app.services.personal import PersonalCollectionService

    return PersonalCollectionService


def seed(backend):
    actors = {}
    for label, role in (("owner", "member"), ("other", "member"), ("admin", "admin")):
        token = uuid.uuid4().hex * 2
        account = {
            "id": str(uuid.uuid4()),
            "username": "personal_" + uuid.uuid4().hex[:12],
            "display_name": "Membre",
            "password_hash": "$argon2id$fixture",
            "role": role,
            "enabled": True,
            "created_at": 0,
        }
        session = {
            "id": str(uuid.uuid4()),
            "user_id": account["id"],
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": NOW,
            "last_seen": NOW,
            "expires_at": NOW + 28800,
        }
        backend.run(lambda work: (work.accounts.insert(account), work.sessions.insert(session)))
        actors[label] = (account["id"], token)
    movie = {
        "id": str(uuid.uuid4()),
        "title": "Personnel " + uuid.uuid4().hex[:8],
        "year": 2026,
        "director": "Auteur",
        "actors": [],
        "genres": [],
        "synopsis": "",
    }
    backend.run(lambda work: work.catalogue.insert_movie(movie))
    return actors, movie


def event(actor, *, timestamp=NOW):
    return {
        "id": str(uuid.uuid4()),
        "actor_id": actor,
        "entity_type": "collection",
        "entity_id": str(uuid.uuid4()),
        "action": "collection.created",
        "timestamp": timestamp,
    }


def exercise_personal_commands(backend, *, scope_observer=None):
    service = implementation()(backend)
    actors, movie = seed(backend)
    if scope_observer is not None:
        scope_observer.clear()
    actor, token = actors["owner"]
    body = {"movie_id": movie["id"], "kind": "custom", "list_name": "Straße Σς"}
    before = backend.query(lambda work: work.revision)
    assert service.create("invalid", body, timestamp=NOW).status == 401
    assert backend.query(lambda work: work.revision) == before
    first = service.create(token, body, timestamp=NOW)
    assert first.status == 201 and first.body["owner_id"] == actor
    identity, version = first.body["id"], first.headers["ETag"]
    assert service.create(token, {**body, "list_name": "STRASSE ΣΣ"}, timestamp=NOW).status == 409
    for label in ("other", "admin"):
        foreign = actors[label][1]
        assert service.get(foreign, identity, timestamp=NOW).status == 404
        assert service.delete(foreign, identity, if_match=version, timestamp=NOW).status == 404
        assert service.entries(foreign, [], timestamp=NOW).body["total"] == 0
    assert (
        service.create(
            actors["other"][1], {**body, "list_name": "STRASSE ΣΣ"}, timestamp=NOW + 1
        ).status
        == 201
    )
    listing = service.entries(token, [("list_name", "STRASSE ΣΣ"), ("limit", "1")], timestamp=NOW)
    assert listing.body["total"] == 1 and listing.body["entries"][0] == first.body
    assert set(listing.body["movies"]) == {movie["id"]}
    assert service.replace(token, identity, {}, if_match=None, timestamp=NOW).status == 428
    assert service.replace(token, identity, {}, if_match='"old"', timestamp=NOW).status == 412
    changed = service.replace(
        token, identity, {"list_name": "ΐ" * 80, "supports": []}, if_match=version, timestamp=NOW
    )
    assert changed.status == 200 and changed.headers["ETag"] != version
    assert service.entries(token, [("list_name", "ΐ" * 80)], timestamp=NOW).body["total"] == 1
    assert service.delete(token, identity, if_match=version, timestamp=NOW).status == 412
    assert (
        service.delete(token, identity, if_match=changed.headers["ETag"], timestamp=NOW).status
        == 204
    )
    assert service.get(token, identity, timestamp=NOW).status == 404
    assert service.get(token, "not-uuid", timestamp=NOW).status == 404
    assert service.audit(token, [("unknown", "x")], timestamp=NOW).status == 403
    audit = service.audit(actors["admin"][1], [], timestamp=NOW)
    own = [item for item in audit.body["events"] if item["entity_id"] == identity]
    assert [item["action"] for item in own] == [
        "collection.deleted",
        "collection.updated",
        "collection.created",
    ]
    assert all(
        set(item) == {"id", "actor_id", "action", "entity_type", "entity_id", "timestamp"}
        for item in own
    )
    assert "Straße" not in repr(own) and "supports" not in repr(own)
    assert backend.query(lambda work: work.personal.get_owned(actor, identity)) is None
    pending = {
        "id": str(uuid.uuid4()),
        "owner_id": actor,
        "movie_id": movie["id"],
        "kind": "favorite",
        "list_name": "",
        "supports": [],
        "created_at": NOW,
    }
    invalid = event(str(uuid.uuid4()))
    before = backend.query(lambda work: work.revision)
    try:
        backend.run(lambda work: (work.personal.insert(pending), work.events.append(invalid)))
    except ValueError:
        pass
    else:
        raise AssertionError("Missing audit actor was accepted")
    assert backend.query(lambda work: work.revision) == before
    assert backend.query(lambda work: work.personal.get_owned(actor, pending["id"])) is None
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _: service.create(
                    token, {"movie_id": movie["id"], "kind": "watchlist"}, timestamp=NOW
                ),
                range(2),
            )
        )
    assert sorted(item.status for item in outcomes) == [201, 409]
    created_id = next(item.body["id"] for item in outcomes if item.status == 201)
    events, _ = backend.query(lambda work: work.events.page(100, 0))
    assert len([item for item in events if item["entity_id"] == created_id]) == 1
    checked = verify_private_trace(scope_observer)
    return {
        "personal_service": "passed",
        "owner_admin_privacy": True,
        "unicode_duplicate_and_filter": True,
        "atomic_minimal_append_order_audit": True,
        "invalid_actor_aborts_entry_and_event": True,
        "concurrent_duplicate_one_entry_one_event": True,
        "native_owner_page_trace": {"checked": scope_observer is not None, "commands": checked},
    }


def verify_private_trace(events):
    if events is None:
        return 0
    assert events, "Private native trace is empty"
    for command, collection, owner_scoped, identity_scoped, ids_count in events:
        assert collection in {
            "users",
            "sessions",
            "coordination",
            "entity_ids",
            "movies",
            "collection_entries",
            "audit_events",
        }, "Private operation read another domain"
        if collection == "collection_entries":
            if command in ("find", "aggregate"):
                assert owner_scoped or identity_scoped, (
                    "Private page/count lacks owner or exact identity"
                )
            elif command in ("update", "delete"):
                assert owner_scoped and identity_scoped, (
                    "Private mutation lacks ownership predicate"
                )
        if collection == "movies" and command == "find":
            assert identity_scoped or (ids_count is not None and ids_count <= 100), (
                "Private page loads films outside its selected identities"
            )
    return len(events)


class PersonalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.json"
        V2Store(self.path).initialize()
        self.backend = JsonPersistence(self.path)

    def test_shared_private_contract_on_real_json(self):
        self.assertEqual(exercise_personal_commands(self.backend)["personal_service"], "passed")

    def test_native_trace_rejects_unscoped_private_pages_and_unbounded_movie_reads(self):
        for wrong in (
            [("find", "collection_entries", False, False, None)],
            [("update", "collection_entries", False, True, None)],
            [("find", "movies", False, False, None)],
            [("find", "movies", False, False, 101)],
            [("find", "loans", False, True, None)],
        ):
            with self.assertRaises(AssertionError):
                verify_private_trace(wrong)

    def test_audit_keeps_append_order_across_same_or_decreasing_timestamps_and_2000_boundary(self):
        actors, _ = seed(self.backend)
        actor = actors["owner"][0]
        records = [event(actor, timestamp=NOW + (number % 2)) for number in range(2000)]
        V2Store(self.path).transact(lambda document: document["audit_events"].extend(records))
        last = event(actor, timestamp=1)
        self.backend.run(lambda work: work.events.append(last))
        page, total = self.backend.query(lambda work: work.events.page(100, 0))
        self.assertEqual(total, 2000)
        self.assertEqual(page, list(reversed([*records[1:], last]))[:100])
        self.assertEqual(V2Store(self.path).read()["audit_events"], [*records[1:], last])
        next_event = event(actor, timestamp=0)
        first_append = {**last, "id": str(uuid.uuid4())}
        self.backend.run(
            lambda work: (work.events.append(first_append), work.events.append(next_event))
        )
        page, total = self.backend.query(lambda work: work.events.page(1, 0))
        self.assertEqual((page, total), ([next_event], 2000))

    def test_rollback_and_retry_keep_entry_and_event_identities_once(self):
        actors, movie = seed(self.backend)
        observed, backend = [], self.backend

        class Abort(Exception):
            pass

        class Retry:
            def run(self, callback):
                def apply(work, abort):
                    result = callback(work)
                    events, _ = work.events.page(1, 0)
                    observed.append((result.body["id"], events[0]["id"]))
                    if abort:
                        raise Abort()
                    return result

                try:
                    backend.run(lambda work: apply(work, True))
                except Abort:
                    pass
                return backend.run(lambda work: apply(work, False))

        result = implementation()(Retry()).create(
            actors["owner"][1], {"movie_id": movie["id"], "kind": "favorite"}, timestamp=NOW
        )
        self.assertEqual(result.status, 201)
        self.assertEqual(observed[0], observed[1])
        document = V2Store(self.path).read()
        self.assertEqual((len(document["collections"]), len(document["audit_events"])), (1, 1))

    def test_readonly_closed_and_selector_guards_and_detached_values(self):
        from app.personal_model import PersonalQuery

        actors, movie = seed(self.backend)
        owner = actors["owner"][0]
        record = {
            "id": str(uuid.uuid4()),
            "owner_id": owner,
            "movie_id": movie["id"],
            "kind": "owned",
            "list_name": "",
            "supports": ["dvd"],
            "created_at": NOW,
        }
        self.backend.run(lambda work: work.personal.insert(record))
        with self.assertRaises(ValueError):
            self.backend.query(lambda work: work.personal.page({"$ne": None}, PersonalQuery()))
        with self.assertRaises(ValueError):
            self.backend.query(lambda work: work.personal.get_owned(owner, {"$ne": None}))
        with self.assertRaises(RuntimeError):
            self.backend.query(lambda work: work.events.append(event(owner)))
        borrowed = self.backend.query(lambda work: work.personal)
        with self.assertRaises(RuntimeError):
            borrowed.get_owned(owner, record["id"])
        copied = self.backend.query(lambda work: work.personal.get_owned(owner, record["id"]))
        copied["supports"].clear()
        self.assertEqual(
            self.backend.query(lambda work: work.personal.get_owned(owner, record["id"]))[
                "supports"
            ],
            ["dvd"],
        )

    def test_json_physical_collection_capacity_is_preserved_without_claiming_fair_admission(self):
        actors, movie = seed(self.backend)
        owner, token = actors["owner"]
        records = [
            {
                "id": str(uuid.uuid4()),
                "owner_id": owner,
                "movie_id": movie["id"],
                "kind": "custom",
                "list_name": str(number),
                "supports": [],
                "created_at": NOW,
            }
            for number in range(10000)
        ]
        V2Store(self.path).transact(lambda document: document["collections"].extend(records))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            implementation()(self.backend).create(
                token, {"movie_id": movie["id"], "kind": "favorite"}, timestamp=NOW
            )
        self.assertEqual(self.path.read_bytes(), before)


def exercise_personal_service(database, *, scope_observer=None):
    from bson import Int64
    from pymongo.errors import DuplicateKeyError, OperationFailure

    from app.persistence.mongo import MongoPersistence
    from app.persistence.mongo_model import encode_record

    backend = MongoPersistence(database)
    result = exercise_personal_commands(backend, scope_observer=scope_observer)
    actors, _ = seed(backend)
    actor = actors["owner"][0]
    last = database.audit_events.find_one({}, {"_ordinal": 1}, sort=[("_ordinal", -1)])
    start = int(last["_ordinal"]) if last else 0
    records = [event(actor, timestamp=NOW + (number % 2)) for number in range(2501)]
    encoded = [
        encode_record("audit_events", row, audit_ordinal=start + number + 1)
        for number, row in enumerate(records)
    ]
    database.audit_events.insert_many(encoded)
    database.entity_ids.insert_many([{"_id": row["id"], "kind": "audit_events"} for row in records])
    final = event(actor, timestamp=1)
    backend.run(lambda work: work.events.append(final))
    kept = list(database.audit_events.find({}, {"_id": 1, "_ordinal": 1}).sort("_ordinal", 1))
    expected = [row["id"] for row in records[-1999:]] + [final["id"]]
    assert [row["_id"] for row in kept] == expected
    assert database.entity_ids.count_documents({"kind": "audit_events"}) == 2000
    assert (
        database.entity_ids.count_documents(
            {"_id": {"$in": [row["id"] for row in records[:-1999]]}}
        )
        == 0
    )
    page, total = backend.query(lambda work: work.events.page(100, 0))
    assert total == 2000 and [row["id"] for row in page] == list(reversed(expected))[:100]
    first_extra, second_extra = event(actor, timestamp=NOW + 5), event(actor, timestamp=0)
    backend.run(lambda work: (work.events.append(first_extra), work.events.append(second_extra)))
    page, total = backend.query(lambda work: work.events.page(2, 0))
    assert total == 2000 and page == [second_extra, first_extra]
    result["multiple_appends_preserve_order_in_one_transaction"] = True
    for field in ("owner_id", "movie_id"):
        invalid_entry = {
            "id": str(uuid.uuid4()),
            "owner_id": actor,
            "movie_id": database.movies.find_one({}, {"_id": 1})["_id"],
            "kind": "favorite",
            "list_name": "",
            "supports": [],
            "created_at": NOW,
            field: str(uuid.uuid4()),
        }
        before_revision = backend.query(lambda work: work.revision)
        try:
            backend.run(lambda work: work.personal.insert(invalid_entry))
        except ValueError:
            pass
        else:
            raise AssertionError("Missing collection parent was accepted")
        assert backend.query(lambda work: work.revision) == before_revision
        assert database.entity_ids.count_documents({"_id": invalid_entry["id"]}) == 0
        assert database.collection_entries.count_documents({"_id": invalid_entry["id"]}) == 0
    raw = database.audit_events.find_one({"_id": final["id"]})
    duplicate = {**raw, "_id": str(uuid.uuid4())}
    try:
        database.audit_events.insert_one(duplicate)
    except DuplicateKeyError:
        pass
    else:
        raise AssertionError("Duplicate audit ordinal was accepted")
    invalid = {key: value for key, value in duplicate.items() if key != "_ordinal"}
    try:
        database.audit_events.insert_one(invalid)
    except OperationFailure as exc:
        assert exc.code == 121
    else:
        raise AssertionError("Missing audit ordinal was accepted")
    database.audit_events.update_one({"_id": raw["_id"]}, {"$set": {"_ordinal": Int64(2**63 - 1)}})
    before = backend.query(lambda work: work.revision)
    overflow_event = event(actor)
    try:
        try:
            backend.run(lambda work: work.events.append(overflow_event))
        except ValueError:
            pass
        else:
            raise AssertionError("Exhausted audit ordinal was reset")
        assert backend.query(lambda work: work.revision) == before
    finally:
        database.audit_events.update_one(
            {"_id": raw["_id"]}, {"$set": {"_ordinal": raw["_ordinal"]}}
        )
    result.update(
        audit_retention_exact=2000,
        audit_multi_batch_registry_release=True,
        native_unique_ordinal_and_validation_refused=True,
        audit_ordinal_exhaustion_aborts=True,
        missing_collection_parent_aborts_and_releases_registry=True,
    )
    return result


def test_personal_http_uses_pure_services_and_preserves_authorization_parse_order(
    tmp_path, monkeypatch
):
    from app import create_app
    from app.services.results import ServiceResult

    path = tmp_path / "store.json"
    V2Store(path).initialize()
    backend = JsonPersistence(path)
    actors, movie = seed(backend)
    app = create_app(
        {
            "TESTING": True,
            "DATA_FILE": str(tmp_path / "legacy.json"),
            "ENTERPRISE_ENABLED": True,
            "ENTERPRISE_DATA_FILE": str(path),
            "AUTH_THROTTLE_KEY": b"x" * 32,
            "AUTH_CLOCK": lambda: NOW,
        }
    )
    client = app.test_client()
    headers = {label: {"Authorization": "Bearer " + item[1]} for label, item in actors.items()}
    original = app.extensions["enterprise_persistence"].run

    def observed(command):
        def pure(work):
            result = command(work)
            assert isinstance(result, ServiceResult)
            return result

        return original(pure)

    def legacy(*args, **kwargs):
        raise AssertionError("Private endpoint used the legacy document mutation")

    monkeypatch.setattr(app.extensions["enterprise_persistence"], "run", observed)
    monkeypatch.setattr(app.extensions["enterprise_store"], "transact", legacy)
    assert client.post("/api/v1/collections", data="broken").status_code == 401
    assert client.get("/api/v1/collections?unknown=x").status_code == 401
    assert client.get("/api/v1/audit-events?unknown=x", headers=headers["owner"]).status_code == 403
    first = client.post(
        "/api/v1/collections",
        headers=headers["owner"],
        json={"movie_id": movie["id"], "kind": "favorite"},
    )
    assert first.status_code == 201
    endpoint = first.headers["Location"]
    for label in ("other", "admin"):
        assert client.put(endpoint, headers=headers[label], data="broken").status_code == 404
    assert client.put(endpoint, headers=headers["owner"], data="broken").status_code == 428
    assert (
        client.put(
            endpoint, headers={**headers["owner"], "If-Match": '"stale"'}, data="broken"
        ).status_code
        == 412
    )
    assert (
        client.put(
            endpoint,
            headers={**headers["owner"], "If-Match": first.headers["ETag"]},
            json={"list_name": "", "supports": []},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/collections", headers=headers["owner"]).json["total"] == 1
    assert client.get(endpoint, headers=headers["owner"]).status_code == 200
    assert (
        client.delete(
            endpoint, headers={**headers["owner"], "If-Match": first.headers["ETag"]}
        ).status_code
        == 204
    )
    assert client.get("/api/v1/collections/not-uuid", headers=headers["owner"]).status_code == 404
    audit = client.get("/api/v1/audit-events?limit=1&limit=2", headers=headers["admin"])
    assert audit.status_code == 200 and audit.json["limit"] == 1
