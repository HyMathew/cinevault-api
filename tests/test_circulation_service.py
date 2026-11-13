"""The same circulation policy over real JSON and an explicitly supplied native DB."""

import hashlib
import importlib
import importlib.util
import tempfile
import unittest
import uuid

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store
from app.validation import ValidationError

NOW = 1000


def seed(backend):
    actors = {}
    for label, role in (("owner", "member"), ("other", "member"), ("admin", "admin")):
        identity, token = str(uuid.uuid4()), uuid.uuid4().hex * 2
        account = {
            "id": identity,
            "username": "circulation_" + uuid.uuid4().hex[:12],
            "display_name": "Membre fictif",
            "role": role,
            "enabled": True,
            "password_hash": "$argon2id$fixture",
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
        backend.run(lambda work: (work.accounts.insert(account), work.sessions.insert(session)))
        actors[label] = (identity, token)
    movie = {
        "id": str(uuid.uuid4()),
        "title": "Circulation " + uuid.uuid4().hex[:12],
        "year": 2026,
        "director": "Auteur",
        "actors": [],
        "genres": [],
        "synopsis": "",
    }
    backend.run(lambda work: work.catalogue.insert_movie(movie))
    return actors, movie


def implementation():
    assert importlib.util.find_spec("app.services.circulation") is not None, (
        "Shared circulation service and repository ports are required"
    )
    return importlib.import_module("app.services.circulation").CirculationService


def lending_fixture(backend):
    actors, movie = seed(backend)
    item = {
        "id": str(uuid.uuid4()),
        "movie_id": movie["id"],
        "support": "dvd",
        "condition": "good",
        "location": "A",
    }
    backend.run(lambda work: work.catalogue.insert_copy(item))
    return actors, movie, item


def exercise_circulation_commands(backend, *, observer=None):
    service = implementation()(backend)
    actors, movie, item = lending_fixture(backend)
    owner, other, admin = (actors[name][1] for name in ("owner", "other", "admin"))
    if observer is not None:
        observer.clear()
    before = backend.query(lambda work: work.revision)
    assert service.request_loan("invalid", {}, timestamp=NOW).status == 401
    assert backend.query(lambda work: work.revision) == before
    data = {"movie_id": movie["id"]}
    created = service.request_loan(owner, data, key="circulation-loan-key-001", timestamp=NOW)
    assert created.status == 201 and created.body["borrower_id"] == actors["owner"][0]
    again = service.request_loan(owner, data, key="circulation-loan-key-001", timestamp=NOW)
    assert again.status == 201 and again.body == created.body
    assert again.headers["Idempotency-Replayed"] == "true"
    identity, version = created.body["id"], created.headers["ETag"]
    assert service.loan(other, identity, timestamp=NOW).status == 404
    assert service.loans(other, [], timestamp=NOW).body["total"] == 0
    assert service.request_loan(owner, data, timestamp=NOW).error["code"] == "open_loan"
    approval = {"copy_id": item["id"], "due_at": NOW + 60}
    assert service.approve(owner, identity, approval, if_match=version, timestamp=NOW).status == 403
    assert service.approve(admin, identity, approval, if_match=None, timestamp=NOW).status == 428
    active = service.approve(admin, identity, approval, if_match=version, timestamp=NOW)
    assert active.status == 200 and active.body["copy_id"] == item["id"]
    waiting = service.create_reservation(
        other, data, key="circulation-reservation-key-001", timestamp=NOW
    )
    assert waiting.status == 201
    later = service.create_reservation(admin, data, timestamp=NOW)
    assert later.status == 201 and later.body["sequence"] > waiting.body["sequence"]
    returned = service.return_loan(
        admin, identity, if_match=active.headers["ETag"], timestamp=NOW + 1
    )
    assert returned.status == 200 and returned.body["status"] == "returned"
    available = service.availability([("movie_id", movie["id"])], timestamp=NOW + 1)
    assert available.body == {
        "movie_id": movie["id"],
        "total": 1,
        "loaned": 0,
        "free": 1,
        "reserved": 2,
        "available": 0,
    }
    pending = service.request_loan(other, data, timestamp=NOW + 1)
    third = service.request_loan(admin, data, timestamp=NOW + 1)
    assert service.options(admin, third.body["id"], timestamp=NOW + 1).body["copies"] == []
    refused = service.approve(
        admin, third.body["id"], approval, if_match=third.headers["ETag"], timestamp=NOW + 1
    )
    assert refused.status == 409 and refused.error["code"] == "reservation_priority"
    listing = service.reservations(other, [], timestamp=NOW + 1).body
    assert listing["positions"] == {waiting.body["id"]: 1}
    assert listing["ready_ids"] == [waiting.body["id"]]
    assert listing["open_loan_ids"] == {waiting.body["id"]: pending.body["id"]}
    approved = service.approve(
        admin, pending.body["id"], approval, if_match=pending.headers["ETag"], timestamp=NOW + 1
    )
    assert approved.status == 200
    fulfilled = service.reservation(other, waiting.body["id"], timestamp=NOW + 1)
    assert (
        fulfilled.body["status"] == "fulfilled" and fulfilled.body["loan_id"] == pending.body["id"]
    )
    assert (
        service.create_reservation(
            other, data, key="circulation-reservation-key-001", timestamp=NOW + 1
        ).body
        == waiting.body
    )
    assert (
        service.request_loan(owner, data, key="circulation-loan-key-001", timestamp=NOW + 1).body
        == created.body
    )
    cancelled = service.cancel_reservation(
        admin, later.body["id"], if_match=later.headers["ETag"], timestamp=NOW + 1
    )
    assert cancelled.status == 200 and cancelled.body["status"] == "cancelled"
    assert service.loans(other, [("movie_id", "not-an-id")], timestamp=NOW + 1).body["total"] == 0
    events, _ = backend.query(lambda work: work.events.page(100, 0))
    actions = [event["action"] for event in reversed(events)]
    assert actions.index("reservation.fulfilled") < actions.index(
        "loan.approved", actions.index("reservation.fulfilled")
    )
    assert all(
        set(event) == {"id", "actor_id", "entity_type", "entity_id", "action", "timestamp"}
        for event in events
    )
    return {"circulation_service": "passed", "fifo_and_receipts": True}


class CirculationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = __import__("pathlib").Path(self.temp.name) / "store.json"
        V2Store(self.path).initialize()
        self.backend = JsonPersistence(self.path)

    def test_shared_contract_on_real_json_file(self):
        self.assertEqual(
            exercise_circulation_commands(self.backend)["circulation_service"], "passed"
        )

    def test_expiration_commits_with_normal_refusal_but_rolls_back_with_payload_exception(self):
        service = implementation()(self.backend)
        actors, movie, _ = lending_fixture(self.backend)
        entry = {
            "id": str(uuid.uuid4()),
            "movie_id": movie["id"],
            "user_id": actors["other"][0],
            "status": "waiting",
            "sequence": 0,
            "created_at": NOW,
            "expires_at": NOW + 10,
            "closed_at": None,
            "loan_id": None,
        }

        def insert(work):
            entry["sequence"] = work.revision
            work.circulation.insert_reservation(entry)

        self.backend.run(insert)
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            service.cancel_reservation(
                actors["other"][1], entry["id"], body=b"{}", if_match='"old"', timestamp=NOW + 10
            )
        self.assertEqual(self.path.read_bytes(), before)
        result = service.cancel_reservation(
            actors["other"][1], entry["id"], if_match='"old"', timestamp=NOW + 10
        )
        self.assertEqual(result.status, 412)
        doc = V2Store(self.path).read()
        self.assertEqual(doc["reservations"][0]["status"], "expired")
        self.assertEqual(doc["audit_events"][-1]["actor_id"], None)


def verify_circulation_trace(events):
    if events is None:
        return 0
    assert events, "Circulation trace is empty"
    for command, collection, owner, movie, identity, ids_count, pairs, limit, expiration in events:
        assert collection in {
            "users",
            "sessions",
            "coordination",
            "entity_ids",
            "movies",
            "copies",
            "loans",
            "reservations",
            "audit_events",
            "idempotency_records",
        }
        if collection in ("loans", "reservations") and command in ("find", "aggregate"):
            assert (
                owner or movie or identity or pairs is not None or limit is not None or expiration
            ), "Circulation query lacks a business filter or bounded authorized page"
        if collection in ("movies", "users") and command == "find":
            assert identity or ids_count is not None and ids_count <= 100, (
                "Circulation page loaded unrelated parents"
            )
        if pairs is not None:
            assert 1 <= pairs <= 100, "Circulation page pairs exceed their bound"
        if limit is not None and limit != "count":
            assert type(limit) is int and 1 <= limit <= 100, "Unbounded circulation page"
    return len(events)


def exercise_circulation_service(database, *, observer=None, process_config=None):
    from circulation_native import (
        exercise_native_commit_uncertainty,
        exercise_native_expiration_batches,
        exercise_native_query_plans,
    )
    from test_circulation_atomicity import exercise_circulation_atomicity
    from test_circulation_concurrency import exercise_circulation_races
    from test_circulation_constraints import exercise_circulation_constraints
    from test_circulation_integrity import (
        exercise_collision_integrity,
        exercise_expiration_integrity,
        exercise_receipt_integrity,
        exercise_reference_integrity,
        exercise_retry_integrity,
        exercise_surplus_rules,
    )

    from app.persistence.mongo import MongoPersistence

    backend = MongoPersistence(database)
    result = exercise_circulation_commands(backend, observer=observer)
    result["native_targeted_trace"] = {
        "checked": observer is not None,
        "commands": verify_circulation_trace(observer),
    }
    for exercise in (
        exercise_expiration_integrity,
        exercise_receipt_integrity,
        exercise_reference_integrity,
        exercise_retry_integrity,
        exercise_collision_integrity,
        exercise_circulation_constraints,
        exercise_surplus_rules,
    ):
        result.update(exercise(backend))
    result.update(exercise_native_commit_uncertainty(database))
    result.update(
        exercise_circulation_atomicity(
            backend,
            registry_count=lambda: database.entity_ids.count_documents({}),
        )
    )
    result.update(exercise_native_expiration_batches(database))
    result.update(exercise_native_query_plans(database))
    if process_config is None:
        result["native_process_races"] = {"checked": False, "reason": "process_config absent"}
    else:
        result["native_process_races"] = {
            "checked": True,
            **exercise_circulation_races(backend, process_config),
        }
    return result
