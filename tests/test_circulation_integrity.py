"""Rollback, expiration and durable receipt assertions shared by real engines."""

import copy
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_circulation_service import NOW, implementation, lending_fixture, seed

from app.circulation_model import CirculationQuery, PayloadError, RequestBody
from app.persistence.errors import CommitOutcomeUnknown
from app.persistence.json import JsonPersistence
from app.receipt_storage import digest
from app.services.receipts import key_digest
from app.storage_v2 import V2Store, validate_document
from app.validation import ValidationError


def waiting_entries(backend, actors, movie):
    entries = []
    for label in ("owner", "other", "admin"):
        record = {
            "id": str(uuid.uuid4()),
            "movie_id": movie["id"],
            "user_id": actors[label][0],
            "status": "waiting",
            "sequence": 0,
            "created_at": NOW,
            "expires_at": NOW + 10,
            "closed_at": None,
            "loan_id": None,
        }

        def insert(work):
            record["sequence"] = work.revision
            work.circulation.insert_reservation(record)

        backend.run(insert)
        entries.append(record)
    return entries


def exercise_expiration_integrity(backend, *, reorder=None):
    service = implementation()(backend)
    actors, movie = seed(backend)
    entries = waiting_entries(backend, actors, movie)
    prior = {
        "id": str(uuid.uuid4()),
        "actor_id": actors["admin"][0],
        "entity_type": "collection",
        "entity_id": str(uuid.uuid4()),
        "action": "collection.deleted",
        "timestamp": NOW + 999,
    }
    backend.run(lambda work: work.events.append(prior))
    if reorder is not None:
        reorder()
    before = backend.query(lambda work: work.revision)
    malformed = RequestBody(b"{", True)
    try:
        service.create_reservation(actors["owner"][1], malformed, timestamp=NOW + 10)
    except PayloadError as exc:
        assert exc.status == 400
    else:
        raise AssertionError("Malformed body was committable")
    assert backend.query(lambda work: work.revision) == before
    assert all(
        backend.query(lambda work, identity=row["id"]: work.circulation.reservation(identity))[
            "status"
        ]
        == "waiting"
        for row in entries
    )
    # A masked foreign404 is a normal business result and still commits maintenance.
    observed = service.reservation(actors["other"][1], entries[0]["id"], timestamp=NOW + 10)
    assert observed.status == 404
    final = [
        backend.query(lambda work, identity=row["id"]: work.circulation.reservation(identity))
        for row in entries
    ]
    assert all(row["status"] == "expired" and row["closed_at"] == NOW + 10 for row in final)
    assert [row["sequence"] for row in final] == [row["sequence"] for row in entries]
    events, _ = backend.query(lambda work: work.events.page(100, 0))
    expired = [
        row for row in reversed(events) if row["entity_id"] in {item["id"] for item in entries}
    ]
    assert [row["entity_id"] for row in expired] == [row["id"] for row in entries]
    assert all(row["actor_id"] is None and row["timestamp"] == NOW + 10 for row in expired)
    assert next(row for row in events if row["id"] == prior["id"]) == prior
    return {"expiration_order_and_exception_rollback": "passed"}


def exercise_receipt_integrity(backend):
    service = implementation()(backend)
    actors, movie, item = lending_fixture(backend)
    owner, other = actors["owner"][1], actors["other"][1]
    data = {"movie_id": movie["id"]}
    key = "circulation-shared-actor-key-001"
    first = service.request_loan(owner, data, key=key, timestamp=NOW)
    second_actor = service.request_loan(other, data, key=key, timestamp=NOW)
    assert (
        first.status == second_actor.status == 201 and first.body["id"] != second_actor.body["id"]
    )
    conflict = service.create_reservation(owner, data, key=key, timestamp=NOW)
    assert conflict.status == 409 and conflict.error["code"] == "operation_conflict"
    altered = service.request_loan(owner, {"movie_id": str(uuid.uuid4())}, key=key, timestamp=NOW)
    assert altered.status == 409 and altered.error["code"] == "operation_conflict"
    before = backend.query(lambda work: work.revision)
    try:
        service.request_loan(owner, data, key="short", timestamp=NOW)
    except ValidationError:
        pass
    else:
        raise AssertionError("Invalid idempotency key was accepted")
    assert backend.query(lambda work: work.revision) == before
    # Durable commit, then lost acknowledgment: the second request must reuse its receipt.
    actors, movie, _ = lending_fixture(backend)
    token = actors["owner"][1]

    class LostAcknowledgment:
        def run(self, command):
            backend.run(command)
            raise CommitOutcomeUnknown("synthetic acknowledgment loss after durable commit")

    try:
        implementation()(LostAcknowledgment()).request_loan(
            token,
            {"movie_id": movie["id"]},
            key=key,
            timestamp=NOW,
        )
    except CommitOutcomeUnknown:
        pass
    else:
        raise AssertionError("Commit uncertainty was reported as success")
    replay = service.request_loan(token, {"movie_id": movie["id"]}, key=key, timestamp=NOW)
    assert replay.status == 201 and replay.headers["Idempotency-Replayed"] == "true"
    rows = service.loans(token, [], timestamp=NOW).body["loans"]
    assert [row["id"] for row in rows] == [replay.body["id"]]
    actors, movie, _ = lending_fixture(backend)
    token, actor = actors["owner"][1], actors["owner"][0]
    created = service.request_loan(token, {"movie_id": movie["id"]}, key=key, timestamp=NOW)
    cached = backend.query(lambda work: work.receipts.get(actor, key_digest(key)))
    for field, value in (
        ("password_hash", "never-allowed"),
        ("borrower_id", actors["other"][0]),
        ("status", "active"),
        ("_etag", '"' + "0" * 64 + '"'),
    ):
        invalid = copy.deepcopy(cached)
        invalid["id"] = str(uuid.uuid4())
        invalid["key_hash"] = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        invalid["response"][field] = value
        if field != "_etag":
            invalid["response"]["_etag"] = (
                '"' + digest({k: v for k, v in invalid["response"].items() if k != "_etag"}) + '"'
            )
        before = backend.query(lambda work: work.revision)
        try:
            backend.run(lambda work: work.receipts.insert(invalid))
        except ValueError:
            pass
        else:
            raise AssertionError("Forged or non-minimal circulation receipt was accepted")
        assert backend.query(lambda work: work.revision) == before
        assert backend.query(lambda work: work.receipts.get(actor, invalid["key_hash"])) is None
    assert (
        service.cancel_loan(
            token, created.body["id"], if_match=created.headers["ETag"], timestamp=NOW + 1
        ).status
        == 200
    )
    late = NOW + 86400 - 1
    new_session = {
        "id": str(uuid.uuid4()),
        "user_id": actor,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "created_at": late,
        "last_seen": late,
        "expires_at": late + 28800,
    }
    backend.run(lambda work: (work.sessions.revoke_user(actor), work.sessions.insert(new_session)))
    old = service.request_loan(token, {"movie_id": movie["id"]}, key=key, timestamp=late)
    assert old.status == 201 and old.body == created.body
    new = service.request_loan(token, {"movie_id": movie["id"]}, key=key, timestamp=late + 1)
    assert new.status == 201 and new.body["id"] != created.body["id"]
    assert (
        backend.query(lambda work: work.circulation.loan(created.body["id"]))["status"]
        == "cancelled"
    )
    disabled = backend.query(lambda work: work.accounts.get(actor))
    backend.run(lambda work: work.accounts.save({**disabled, "enabled": False}))
    before = backend.query(lambda work: work.revision)
    assert (
        service.request_loan(token, {"movie_id": movie["id"]}, key=key, timestamp=late + 1).status
        == 401
    )
    assert backend.query(lambda work: work.revision) == before
    return {"actor_key_and_lost_ack_reconciliation": "passed"}


def exercise_reference_integrity(backend):
    service = implementation()(backend)
    actors, movie, _ = lending_fixture(backend)
    result = service.request_loan(actors["owner"][1], {"movie_id": movie["id"]}, timestamp=NOW)
    identity = result.body["id"]
    for field in ("movie_id", "borrower_id"):
        current = backend.query(lambda work: work.circulation.loan(identity))
        before = backend.query(lambda work: work.revision)
        invalid = {**current, field: str(uuid.uuid4())}
        try:
            backend.run(lambda work: work.circulation.save_loan(invalid))
        except ValueError:
            pass
        else:
            raise AssertionError("Missing loan parent accepted")
        assert backend.query(lambda work: work.revision) == before
        assert backend.query(lambda work: work.circulation.loan(identity)) == current
    # Owner predicates also protect repository writes, independently of the service check.
    before = backend.query(lambda work: work.revision)
    try:
        backend.run(lambda work: work.circulation.save_loan(current, owner_id=actors["other"][0]))
    except LookupError:
        pass
    else:
        raise AssertionError("Foreign loan write accepted")
    assert backend.query(lambda work: work.revision) == before
    borrowed = backend.query(lambda work: work.circulation)
    try:
        borrowed.loan(identity)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Closed circulation repository remained usable")
    actors, movie, item = lending_fixture(backend)
    owner, other, admin = (actors[label][1] for label in ("owner", "other", "admin"))
    service = implementation()(backend)
    requested = service.request_loan(owner, {"movie_id": movie["id"]}, timestamp=NOW)
    assigned = service.approve(
        admin,
        requested.body["id"],
        {"copy_id": item["id"], "due_at": NOW + 60},
        if_match=requested.headers["ETag"],
        timestamp=NOW,
    )
    reserved = service.create_reservation(other, {"movie_id": movie["id"]}, timestamp=NOW)
    assert (
        service.return_loan(
            admin, requested.body["id"], if_match=assigned.headers["ETag"], timestamp=NOW
        ).status
        == 200
    )
    next_loan = service.request_loan(other, {"movie_id": movie["id"]}, timestamp=NOW)
    assert (
        service.approve(
            admin,
            next_loan.body["id"],
            {"copy_id": item["id"], "due_at": NOW + 60},
            if_match=next_loan.headers["ETag"],
            timestamp=NOW,
        ).status
        == 200
    )
    loan = backend.query(lambda work: work.circulation.loan(next_loan.body["id"]))
    reservation = backend.query(lambda work: work.circulation.reservation(reserved.body["id"]))
    assert reservation["status"] == "fulfilled"
    for kind, changed in (
        ("loan", {**loan, "borrower_id": actors["owner"][0]}),
        ("reservation", {**reservation, "user_id": actors["owner"][0]}),
    ):
        before = backend.query(lambda work: work.revision)
        try:
            backend.run(lambda work: getattr(work.circulation, "save_" + kind)(changed))
        except ValueError:
            pass
        else:
            raise AssertionError("Fulfilled inverse relation was broken")
        assert backend.query(lambda work: work.revision) == before
        assert backend.query(lambda work: work.circulation.loan(loan["id"])) == loan
        assert (
            backend.query(lambda work: work.circulation.reservation(reservation["id"]))
            == reservation
        )
    return {"targeted_fk_owner_and_closed_scope": "passed"}


def exercise_retry_integrity(backend):
    actors, movie = seed(backend)
    entries = waiting_entries(backend, actors, movie)
    extra_movie = {**movie, "id": str(uuid.uuid4()), "title": "Arrival " + uuid.uuid4().hex[:12]}
    extra = {**entries[0], "id": str(uuid.uuid4()), "movie_id": extra_movie["id"]}
    observed = []

    class Abort(Exception):
        pass

    class Replay:
        def run(self, callback):
            def apply(work, abort):
                result = callback(work)
                events, _ = work.events.page(100, 0)
                observed.append(
                    {
                        row["entity_id"]: row["id"]
                        for row in events
                        if row["action"] == "reservation.expired"
                        and row["entity_id"] in {entry["id"] for entry in [*entries, extra]}
                    }
                )
                if abort:
                    raise Abort()
                return result

            try:
                backend.run(lambda work: apply(work, True))
            except Abort:
                pass

            def arrival(work):
                work.catalogue.insert_movie(extra_movie)
                work.circulation.insert_reservation({**extra, "sequence": work.revision})

            backend.run(arrival)
            return backend.run(lambda work: apply(work, False))

    result = implementation()(Replay()).reservations(actors["admin"][1], [], timestamp=NOW + 10)
    assert result.status == 200
    assert len(observed[0]) == 3 and len(observed[1]) == 4
    assert all(observed[1][key] == value for key, value in observed[0].items())
    events, _ = backend.query(lambda work: work.events.page(100, 0))
    assert len([row for row in events if row["id"] in observed[1].values()]) == 4
    return {"abort_replay_with_additional_expiration_keeps_ids": "passed"}


def exercise_collision_integrity(backend):
    actors, movie = seed(backend)
    before = backend.query(lambda work: work.revision)
    with patch("app.services.circulation.uuid.uuid4", return_value=uuid.UUID(movie["id"])):
        try:
            implementation()(backend).request_loan(
                actors["owner"][1], {"movie_id": movie["id"]}, timestamp=NOW
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Cross-collection identity collision was accepted")
    assert backend.query(lambda work: work.revision) == before
    assert backend.query(lambda work: work.circulation.loan(movie["id"])) is None
    return {"uuid_collision_aborts_without_regeneration": "passed"}


def exercise_expiration_batches(backend, prepare):
    actors, template = seed(backend)
    base = backend.query(lambda work: work.revision)
    users = [
        {
            "id": str(uuid.uuid4()),
            "username": "batch_" + uuid.uuid4().hex[:12],
            "display_name": "Membre fictif",
            "password_hash": "$argon2id$fixture",
            "role": "member",
            "enabled": True,
            "created_at": 0,
        }
        for _ in range(51)
    ]
    movies = [
        {**template, "id": str(uuid.uuid4()), "title": "Batch " + uuid.uuid4().hex[:12]}
        for _ in range(10)
    ]
    entries = [
        {
            "id": str(uuid.uuid4()),
            "movie_id": movies[index % 10]["id"],
            "user_id": users[index // 10]["id"],
            "status": "waiting",
            "sequence": base + index + 1,
            "created_at": NOW,
            "expires_at": NOW + 10,
            "closed_at": None,
            "loan_id": None,
        }
        for index in range(501)
    ]
    # Operator-owned fixture only; normal service creation still uses work.revision.
    prepare(users, movies, entries, base + 501)
    sizes = backend.query(
        lambda work: [len(batch) for batch in work.circulation.expired_batches(NOW + 10)]
    )
    assert sizes == [500, 1]
    before = backend.query(lambda work: work.revision)
    response = implementation()(backend).reservations(actors["admin"][1], [], timestamp=NOW + 10)
    assert response.status == 200
    assert backend.query(lambda work: work.revision) == before + 1
    wanted = {row["id"] for row in entries}
    observed = []
    for offset in range(0, 501, 100):
        query = CirculationQuery(status="expired", scope="all", limit=100, offset=offset)
        page, _ = backend.query(lambda work: work.circulation.reservations_page(None, query))
        observed.extend(row for row in page if row["id"] in wanted)
    assert len(observed) == 501
    assert all(row["status"] == "expired" and row["closed_at"] == NOW + 10 for row in observed)
    events, _ = backend.query(lambda work: work.events.page(100, 0))
    assert [row["entity_id"] for row in events[:100]] == [
        row["id"] for row in reversed(entries[-100:])
    ]
    return {"global_expiration_batch_sizes": sizes, "expired_in_one_uow": 501}


def exercise_surplus_rules(backend):
    service = implementation()(backend)
    actors, movie, item = lending_fixture(backend)
    owner, other, admin = (actors[name][1] for name in ("owner", "other", "admin"))
    data = {"movie_id": movie["id"]}
    initial = service.request_loan(owner, data, timestamp=NOW)
    assert (
        service.approve(
            admin,
            initial.body["id"],
            {"copy_id": item["id"], "due_at": NOW + 60},
            if_match=initial.headers["ETag"],
            timestamp=NOW,
        ).status
        == 200
    )
    assert service.create_reservation(other, data, timestamp=NOW).status == 201
    assert service.create_reservation(admin, data, timestamp=NOW).status == 201
    outside, _ = seed(backend)
    token = outside["owner"][1]
    outside_loan = service.request_loan(token, data, timestamp=NOW)
    tail_loan = service.request_loan(admin, data, timestamp=NOW)
    copies = [{**item, "id": str(uuid.uuid4())} for _ in range(4)]
    damaged = {**item, "id": str(uuid.uuid4()), "condition": "damaged"}
    backend.run(
        lambda work: (
            work.catalogue.insert_copy(copies[0]),
            work.catalogue.insert_copy(copies[1]),
            work.catalogue.insert_copy(damaged),
        )
    )
    assert service.availability([("movie_id", movie["id"])], timestamp=NOW).body == {
        "movie_id": movie["id"],
        "total": 4,
        "loaned": 1,
        "free": 2,
        "reserved": 2,
        "available": 0,
    }
    approval = {"copy_id": copies[0]["id"], "due_at": NOW + 60}
    assert (
        service.approve(
            admin,
            outside_loan.body["id"],
            approval,
            if_match=outside_loan.headers["ETag"],
            timestamp=NOW,
        ).error["code"]
        == "reservation_priority"
    )
    backend.run(lambda work: work.catalogue.insert_copy(copies[2]))
    assert (
        service.approve(
            admin,
            outside_loan.body["id"],
            approval,
            if_match=outside_loan.headers["ETag"],
            timestamp=NOW,
        ).status
        == 200
    )
    backend.run(lambda work: work.catalogue.insert_copy(copies[3]))
    tail = service.approve(
        admin,
        tail_loan.body["id"],
        {"copy_id": copies[1]["id"], "due_at": NOW + 60},
        if_match=tail_loan.headers["ETag"],
        timestamp=NOW,
    )
    assert tail.status == 409 and tail.error["code"] == "reservation_priority"
    return {"damaged_inventory_and_strict_surplus_fifo": "passed"}


class CirculationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.json"
        V2Store(self.path).initialize()
        self.backend = JsonPersistence(self.path)

    def test_reordered_valid_snapshot_expires_by_sequence_preserving_prior_history(self):
        def reorder():
            V2Store(self.path).transact(lambda doc: doc["reservations"].reverse())

        self.assertEqual(
            exercise_expiration_integrity(self.backend, reorder=reorder)[
                "expiration_order_and_exception_rollback"
            ],
            "passed",
        )

    def test_receipts_actor_namespace_and_commit_acknowledgment_loss(self):
        self.assertEqual(
            exercise_receipt_integrity(self.backend)["actor_key_and_lost_ack_reconciliation"],
            "passed",
        )

    def test_final_parent_validation_owner_and_scope(self):
        self.assertEqual(
            exercise_reference_integrity(self.backend)["targeted_fk_owner_and_closed_scope"],
            "passed",
        )

    def test_abort_retry_preserves_ids_when_expiration_count_changes(self):
        self.assertEqual(
            exercise_retry_integrity(self.backend)[
                "abort_replay_with_additional_expiration_keeps_ids"
            ],
            "passed",
        )

    def test_identity_collision_rolls_back_without_regenerating_ids(self):
        self.assertEqual(
            exercise_collision_integrity(self.backend)[
                "uuid_collision_aborts_without_regeneration"
            ],
            "passed",
        )

    def test_global_expiration_crosses_500_boundary_in_one_transaction(self):
        def prepare(users, movies, entries, revision):
            document = V2Store(self.path).read()
            document["users"].extend(users)
            document["movies"].extend(movies)
            document["reservations"].extend(entries)
            document["revision"] = revision
            validate_document(document)
            self.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        self.assertEqual(
            exercise_expiration_batches(self.backend, prepare)["expired_in_one_uow"], 501
        )

    def test_inventory_surplus_does_not_allow_waiting_tail_to_skip_head(self):
        self.assertEqual(
            exercise_surplus_rules(self.backend)["damaged_inventory_and_strict_surplus_fifo"],
            "passed",
        )
