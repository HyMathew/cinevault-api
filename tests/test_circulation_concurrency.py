"""Real processes compete against the same owned persistence, then inspect final state."""

import tempfile
import unittest
from pathlib import Path

from circulation_workers import race
from test_circulation_service import NOW, implementation, lending_fixture, seed

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store


def command(method, *args, **kwargs):
    return {"method": method, "args": args, "kwargs": kwargs}


def exercise_circulation_races(backend, configuration):
    service = implementation()(backend)
    actors, movie, item = lending_fixture(backend)
    owner, other, admin = (actors[name][1] for name in ("owner", "other", "admin"))
    data = {"movie_id": movie["id"]}
    loans = [service.request_loan(token, data, timestamp=NOW) for token in (owner, other)]
    approval = {"copy_id": item["id"], "due_at": NOW + 60}
    observed = race(
        configuration,
        [
            command(
                "approve",
                admin,
                row.body["id"],
                approval,
                if_match=row.headers["ETag"],
                timestamp=NOW,
            )
            for row in loans
        ],
    )
    assert sorted(row["status"] for row in observed) == [200, 409]
    current = [
        backend.query(lambda work, identity=row.body["id"]: work.circulation.loan(identity))
        for row in loans
    ]
    assert sorted(row["status"] for row in current) == ["active", "pending"]
    assert sum(row["copy_id"] == item["id"] for row in current) == 1

    actors, movie, item = lending_fixture(backend)
    owner, other, admin = (actors[name][1] for name in ("owner", "other", "admin"))
    data = {"movie_id": movie["id"]}
    observed = race(
        configuration,
        [command("request_loan", owner, data, key="same-process-loan-key-001", timestamp=NOW)] * 2,
    )
    assert [row["status"] for row in observed] == [201, 201]
    assert observed[0]["id"] == observed[1]["id"]
    events, _ = backend.query(lambda work: work.events.page(100, 0))
    assert len([row for row in events if row["entity_id"] == observed[0]["id"]]) == 1
    first = service.loan(owner, observed[0]["id"], timestamp=NOW)
    active = service.approve(
        admin,
        first.body["id"],
        {"copy_id": item["id"], "due_at": NOW + 60},
        if_match=first.headers["ETag"],
        timestamp=NOW,
    )
    assert active.status == 200
    observed = race(configuration, [command("create_reservation", other, data, timestamp=NOW)] * 2)
    assert sorted(row["status"] for row in observed) == [201, 409]

    # A waiting successor makes cancellation of the head remove its allocation priority.
    head = service.reservations(other, [], timestamp=NOW).body["reservations"][0]
    following = service.create_reservation(admin, data, timestamp=NOW)
    assert following.status == 201
    returned = service.return_loan(
        admin, first.body["id"], if_match=active.headers["ETag"], timestamp=NOW + 1
    )
    assert returned.status == 200
    head_loan = service.request_loan(other, data, timestamp=NOW + 1)
    outcomes = race(
        configuration,
        [
            command(
                "cancel_reservation", other, head["id"], if_match=head["_etag"], timestamp=NOW + 1
            ),
            command(
                "approve",
                admin,
                head_loan.body["id"],
                {"copy_id": item["id"], "due_at": NOW + 60},
                if_match=head_loan.headers["ETag"],
                timestamp=NOW + 1,
            ),
        ],
    )
    assert sorted(row["status"] for row in outcomes) in ([200, 409], [200, 412])
    reservation = backend.query(lambda work: work.circulation.reservation(head["id"]))
    loan = backend.query(lambda work: work.circulation.loan(head_loan.body["id"]))
    assert (reservation["status"], loan["status"]) in (
        ("cancelled", "pending"),
        ("fulfilled", "active"),
    )
    assert reservation["loan_id"] == (loan["id"] if loan["status"] == "active" else None)

    actors, movie, item = lending_fixture(backend)
    owner, other, admin = (actors[name][1] for name in ("owner", "other", "admin"))
    data = {"movie_id": movie["id"]}
    original = service.request_loan(owner, data, timestamp=NOW)
    original_active = service.approve(
        admin,
        original.body["id"],
        {"copy_id": item["id"], "due_at": NOW + 60},
        if_match=original.headers["ETag"],
        timestamp=NOW,
    )
    head = service.create_reservation(other, data, timestamp=NOW)
    tail = service.create_reservation(admin, data, timestamp=NOW)
    service.return_loan(
        admin, original.body["id"], if_match=original_active.headers["ETag"], timestamp=NOW + 1
    )
    pending = [service.request_loan(token, data, timestamp=NOW + 1) for token in (other, admin)]
    observed = race(
        configuration,
        [
            command(
                "approve",
                admin,
                row.body["id"],
                {"copy_id": item["id"], "due_at": NOW + 60},
                if_match=row.headers["ETag"],
                timestamp=NOW + 1,
            )
            for row in pending
        ],
    )
    assert sorted(row["status"] for row in observed) == [200, 409]
    first_active = service.loan(other, pending[0].body["id"], timestamp=NOW + 1)
    assert first_active.body["status"] == "active"
    assert (
        service.reservation(other, head.body["id"], timestamp=NOW + 1).body["status"] == "fulfilled"
    )
    assert (
        service.reservation(admin, tail.body["id"], timestamp=NOW + 1).body["status"] == "waiting"
    )
    observed = race(
        configuration,
        [
            command(
                "return_loan",
                admin,
                first_active.body["id"],
                if_match=first_active.headers["ETag"],
                timestamp=NOW + 2,
            ),
            command(
                "approve",
                admin,
                pending[1].body["id"],
                {"copy_id": item["id"], "due_at": NOW + 60},
                if_match=pending[1].headers["ETag"],
                timestamp=NOW + 2,
            ),
        ],
    )
    assert sorted(row["status"] for row in observed) in ([200, 200], [200, 409])
    assert (
        service.loan(other, first_active.body["id"], timestamp=NOW + 2).body["status"] == "returned"
    )
    assert backend.query(lambda work: work.circulation.inventory_counts(movie["id"]))["loaned"] <= 1

    actors, movie = seed(backend)
    reserved = service.create_reservation(
        actors["other"][1], {"movie_id": movie["id"]}, timestamp=NOW
    )
    observed = race(
        configuration,
        [
            command(
                "cancel_reservation",
                actors["other"][1],
                reserved.body["id"],
                if_match=reserved.headers["ETag"],
                timestamp=NOW,
            )
        ]
        * 2,
    )
    assert sorted(row["status"] for row in observed) == [200, 412]
    return {"real_process_races": 7, "last_copy_fifo_cancellation_and_actor_key": "passed"}


class CirculationProcessTests(unittest.TestCase):
    def test_real_processes_preserve_last_copy_fifo_and_actor_key_on_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.json"
            V2Store(path).initialize()
            result = exercise_circulation_races(
                JsonPersistence(path), {"backend": "json", "path": str(path)}
            )
            self.assertEqual(result["real_process_races"], 7)
