"""MongoDB contract; standalone probes need only the locked runtime dependencies."""

import importlib.util
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch


def _id():
    return str(uuid.uuid4())


def _user(username="member", **changes):
    return {
        "id": _id(),
        "username": username,
        "display_name": "Élève",
        "password_hash": "$argon2id$fixture",
        "role": "member",
        "enabled": True,
        "created_at": 0,
        **changes,
    }


def _movie(**changes):
    return {
        "id": _id(),
        "title": "Straße",
        "year": 2026,
        "director": "Auteur",
        "actors": ["Interprète"],
        "synopsis": "",
        "genres": ["Drame"],
        **changes,
    }


def _session(user_id, **changes):
    return {
        "id": _id(),
        "user_id": user_id,
        "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "created_at": 0,
        "last_seen": 0,
        "expires_at": 100,
        **changes,
    }


def _copy(movie_id, **changes):
    return {
        "id": _id(),
        "movie_id": movie_id,
        "support": "dvd",
        "location": "Rayon A",
        "condition": "good",
        **changes,
    }


def _raises(exception, function):
    try:
        function()
    except exception:
        return
    raise AssertionError(f"Expected {exception.__name__}")


def exercise_backend(database, *, maintenance_database=None):
    """Run the real engine contract on a NEW disposable, maintenance-owned DB."""
    from app.persistence.mongo import MongoPersistence
    from app.persistence.mongo_maintenance import provision

    maintenance = database if maintenance_database is None else maintenance_database
    provision(maintenance)
    backend = MongoPersistence(database)
    assert backend.readiness() is None
    assert backend.query(lambda work: work.revision) == 0
    first, film = _user(), _movie()
    item, token = _copy(film["id"]), _session(first["id"])

    def create(work):
        assert work.revision == 1
        # Final reference validation permits dependencies inserted later in the UoW.
        work.sessions.insert(token)
        work.catalogue.insert_copy(item)
        work.accounts.insert(first)
        work.catalogue.insert_movie(film)
        return work.catalogue.movie(film["id"])

    assert backend.run(create) == film
    assert database.entity_ids.count_documents({}) == 4
    assert backend.query(lambda work: work.accounts.by_username("member")) == first
    assert backend.query(lambda work: work.accounts.page(10, 0)) == [first]
    assert backend.query(lambda work: work.sessions.by_token_hash(token["token_hash"])) == token
    assert backend.query(lambda work: work.catalogue.copy(item["id"])) == item
    assert backend.query(lambda work: work.catalogue.copies_for_movie(film["id"])) == [item]
    assert backend.query(
        lambda work: work.catalogue.has_duplicate_movie({**film, "title": "STRASSE"})
    )
    assert not backend.query(
        lambda work: work.catalogue.has_duplicate_movie(film, excluded_id=film["id"])
    )
    read = backend.query(lambda work: work.catalogue.movie(film["id"]))
    read["actors"].clear()
    assert backend.query(lambda work: work.catalogue.movie(film["id"])) == film
    revision = backend.query(lambda work: work.revision)
    assert revision == 1
    retained = backend.query(lambda work: (work, work.accounts))
    _raises(RuntimeError, lambda: retained[0].revision)
    _raises(RuntimeError, lambda: retained[1].get(first["id"]))
    _raises(RuntimeError, lambda: backend.query(lambda work: work.accounts.insert(_user("nope"))))
    _raises(ValueError, lambda: backend.query(lambda work: work.catalogue.movie({"$ne": None})))
    _raises(
        ValueError, lambda: backend.query(lambda work: work.accounts.by_username({"$ne": None}))
    )
    _raises(RuntimeError, lambda: backend.run(lambda work: backend.query(lambda other: None)))
    assert backend.query(lambda work: work.revision) == revision

    def abort(work):
        work.accounts.insert(_user("aborted"))
        raise RuntimeError("abort")

    _raises(RuntimeError, lambda: backend.run(abort))
    assert backend.query(lambda work: work.accounts.by_username("aborted")) is None
    assert database.entity_ids.count_documents({}) == 4
    invalid = [
        lambda work: work.sessions.insert(_session(_id())),
        lambda work: work.catalogue.insert_copy(_copy(_id())),
        lambda work: work.sessions.insert(_session(first["id"], last_seen=101)),
        lambda work: work.sessions.touch(token["id"], 101),
        lambda work: work.catalogue.insert_movie(_movie(year=True)),
        lambda work: work.catalogue.insert_movie(_movie(id=first["id"])),
        lambda work: work.accounts.insert(_user("member")),
        lambda work: work.catalogue.delete_movie(film["id"]),
    ]
    for command in invalid:
        _raises(ValueError, lambda: backend.run(command))
        assert backend.query(lambda work: work.revision) == revision
        assert database.entity_ids.count_documents({}) == 4
    _raises(LookupError, lambda: backend.run(lambda work: work.accounts.save(_user("missing"))))
    _raises(LookupError, lambda: backend.run(lambda work: work.catalogue.save_movie(_movie())))
    _raises(
        LookupError, lambda: backend.run(lambda work: work.catalogue.save_copy(_copy(film["id"])))
    )

    def update(work):
        account = work.accounts.get(first["id"])
        account["display_name"] = "Modifié"
        work.accounts.save(account)
        changed = work.catalogue.movie(film["id"])
        changed["synopsis"] = "Modifié"
        work.catalogue.save_movie(changed)
        changed = work.catalogue.copy(item["id"])
        changed["location"] = "Rayon B"
        work.catalogue.save_copy(changed)
        work.sessions.touch(token["id"], 10)

    backend.run(update)
    assert backend.query(lambda work: work.accounts.get(first["id"]))["display_name"] == "Modifié"
    assert (
        backend.query(lambda work: work.sessions.by_token_hash(token["token_hash"]))["last_seen"]
        == 10
    )
    assert backend.run(lambda work: work.sessions.delete_expired(40, idle_ttl=30)) == 1
    assert database.entity_ids.count_documents({}) == 3
    assert backend.run(lambda work: work.catalogue.delete_copy(item["id"])) is True
    assert backend.run(lambda work: work.catalogue.delete_movie(film["id"])) is True
    assert database.entity_ids.count_documents({}) == 1
    assert backend.query(lambda work: work.catalogue.movie(film["id"])) is None
    before = backend.query(lambda work: work.revision)
    provision(maintenance)
    assert backend.query(lambda work: work.revision) == before
    return {"contract": "passed", "revision": before, "invalid_commands_checked": len(invalid)}


def exercise_references(database):
    """Install valid historical fixtures and test final FK/registry protections."""
    from app.persistence.mongo import MongoPersistence
    from app.persistence.mongo_model import encode_record

    backend = MongoPersistence(database)
    account, film, second = _user("reference"), _movie(), _movie(title="Other")
    item = _copy(film["id"])
    backend.run(
        lambda work: (
            work.accounts.insert(account),
            work.catalogue.insert_movie(film),
            work.catalogue.insert_movie(second),
            work.catalogue.insert_copy(item),
        )
    )
    reservation = {
        "id": _id(),
        "movie_id": film["id"],
        "user_id": account["id"],
        "status": "waiting",
        "sequence": 1,
        "created_at": 0,
        "expires_at": 10,
        "closed_at": None,
        "loan_id": None,
    }
    loan = {
        "id": _id(),
        "movie_id": film["id"],
        "borrower_id": account["id"],
        "copy_id": item["id"],
        "status": "returned",
        "requested_at": 0,
        "approved_at": 0,
        "due_at": 1,
        "returned_at": 2,
        "cancelled_at": None,
    }
    # Operator fixture preparation, not an application persistence path.
    for name, record in (("reservations", reservation), ("loans", loan)):
        database[name].insert_one(encode_record(name, record))
        database.entity_ids.insert_one({"_id": record["id"], "kind": name})
    before = backend.query(lambda work: work.revision)
    for command in (
        lambda work: work.accounts.save({**account, "enabled": False}),
        lambda work: work.catalogue.delete_copy(item["id"]),
        lambda work: work.catalogue.save_copy({**item, "movie_id": second["id"]}),
        lambda work: work.catalogue.delete_movie(film["id"]),
    ):
        _raises(ValueError, lambda: backend.run(command))
        assert backend.query(lambda work: work.revision) == before
    assert backend.query(lambda work: work.catalogue.has_copy_history(item["id"]))
    assert backend.query(lambda work: work.accounts.get(account["id"]))["enabled"] is True

    def hide_driver_failure(work):
        work.accounts.insert(_user("mustrollback"))
        try:
            work.accounts.insert(_user("reference"))
        except ValueError:
            pass

    _raises(ValueError, lambda: backend.run(hide_driver_failure))
    assert backend.query(lambda work: work.accounts.by_username("mustrollback")) is None
    assert backend.query(lambda work: work.revision) == before
    return {"historical_reference_checks": 4, "swallowed_error_aborted": True}


def exercise_concurrency(database):
    """Coordinate two distinct transactions preserving the last active admin."""
    from app.persistence.mongo import MongoPersistence

    backend = MongoPersistence(database)
    accounts = [_user("adminone", role="admin"), _user("admintwo", role="admin")]
    backend.run(lambda work: [work.accounts.insert(account) for account in accounts])

    def demote(account):
        def command(work):
            if not work.accounts.has_other_enabled_admin(account["id"]):
                return "last_admin"
            changed = work.accounts.get(account["id"])
            changed["role"] = "member"
            work.accounts.save(changed)
            return "changed"

        return backend.run(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(demote, accounts))
    assert sorted(outcomes) == ["changed", "last_admin"]
    assert database.users.count_documents({"role": "admin", "enabled": True}) == 1
    return {"concurrent_admin_outcomes": sorted(outcomes)}


class LocalBoundaryTests(unittest.TestCase):
    def test_mongo_adapter_is_an_explicit_non_http_entrypoint(self):
        self.assertIsNotNone(importlib.util.find_spec("app.persistence.mongo"))
        self.assertIsNotNone(importlib.util.find_spec("app.persistence.mongo_maintenance"))

    def test_maintenance_defines_strict_auxiliary_documents(self):
        from app.persistence.mongo_maintenance import auxiliary_schemas

        schemas = auxiliary_schemas()
        self.assertEqual(set(schemas), {"coordination", "entity_ids"})
        self.assertFalse(schemas["coordination"]["additionalProperties"])
        self.assertEqual(schemas["coordination"]["properties"]["revision"]["bsonType"], "long")
        self.assertFalse(schemas["entity_ids"]["additionalProperties"])

    def test_deferred_callbacks_are_rejected_before_driver_access(self):
        from app.persistence.mongo import MongoPersistence

        backend = MongoPersistence(SimpleNamespace(client=object()))

        async def deferred(work):
            return "not executed"

        def generator(work):
            yield "not executed"

        for method in (backend.run, backend.query):
            for callback in (deferred, generator):
                with patch.object(backend._runner, "run") as driver:
                    with self.assertRaisesRegex(RuntimeError, "synchronous"):
                        method(callback)
                    driver.assert_not_called()

    def test_owner_closed_read_only_and_selector_guards_precede_database_access(self):
        from app.persistence.mongo import _MongoWork
        from app.persistence.mongo_repositories import _MongoScope

        scope = _MongoScope(object(), object(), 0, writable=False)
        work = _MongoWork(scope)
        for action in (
            lambda: work.catalogue.movie({"$ne": None}),
            lambda: work.accounts.by_username({"$ne": None}),
            lambda: work.sessions.by_token_hash({"$ne": None}),
        ):
            with self.assertRaises(ValueError):
                action()
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            work.accounts.insert(_user())
        repository = work.catalogue
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.assertRaisesRegex(RuntimeError, "owner"):
                executor.submit(lambda: repository.movie(_id())).result()
        scope.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            _ = work.revision
        with self.assertRaisesRegex(RuntimeError, "closed"):
            repository.movie(_id())

    def test_forked_backend_is_rejected_before_driver_access(self):
        from app.persistence.mongo import MongoPersistence

        backend = MongoPersistence(SimpleNamespace(client=object()))
        with patch("app.persistence.mongo.getpid", return_value=-1):
            with self.assertRaisesRegex(RuntimeError, "owning process"):
                backend.query(lambda work: None)


class RealMongoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        uri = os.environ.get("MONGODB_TEST_URI")
        if not uri:
            raise unittest.SkipTest("MONGODB_TEST_URI absent: real MongoDB contract not executed")
        from pymongo import MongoClient

        cls.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        cls.client.admin.command("ping")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def setUp(self):
        self.name = "cinevault_contract_" + uuid.uuid4().hex
        self.database = self.client[self.name]

    def tearDown(self):
        self.client.drop_database(self.name)

    def test_backend_contract_on_real_engine(self):
        self.assertEqual(exercise_backend(self.database)["contract"], "passed")

    def test_concurrent_admin_commands_on_real_engine(self):
        from app.persistence.mongo_maintenance import provision

        provision(self.database)
        self.assertEqual(len(exercise_concurrency(self.database)["concurrent_admin_outcomes"]), 2)

    def test_final_references_and_hidden_driver_error_on_real_engine(self):
        from app.persistence.mongo_maintenance import provision

        provision(self.database)
        self.assertTrue(exercise_references(self.database)["swallowed_error_aborted"])

    def test_provision_refuses_to_overwrite_existing_validator(self):
        from app.persistence.mongo_maintenance import provision

        provision(self.database)
        self.database.command({"collMod": "users", "validationAction": "warn"})
        with self.assertRaises(ValueError):
            provision(self.database)
        self.assertEqual(self.database.users.options()["validationAction"], "warn")

    def test_missing_coordination_is_not_created_by_application(self):
        from app.persistence.mongo import MongoPersistence

        backend = MongoPersistence(self.database)
        with self.assertRaises(ValueError):
            backend.readiness()
        self.assertEqual(self.database.list_collection_names(), [])


if __name__ == "__main__":
    unittest.main()
