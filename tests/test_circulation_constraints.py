"""Final circulation constraints must also hold when a caller bypasses services."""

import tempfile
import unittest
import uuid
from pathlib import Path

from test_circulation_service import NOW, lending_fixture, seed

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store


def loan_record(user_id, movie_id, *, copy_id=None):
    return {
        "id": str(uuid.uuid4()),
        "movie_id": movie_id,
        "borrower_id": user_id,
        "copy_id": copy_id,
        "status": "active" if copy_id else "pending",
        "requested_at": NOW,
        "approved_at": NOW if copy_id else None,
        "due_at": NOW + 60 if copy_id else None,
        "returned_at": None,
        "cancelled_at": None,
    }


def expect_abort(backend, command):
    before = backend.query(lambda work: work.revision)
    try:
        backend.run(command)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid circulation mutation committed")
    assert backend.query(lambda work: work.revision) == before


def exercise_circulation_constraints(backend):
    actors, movie, item = lending_fixture(backend)
    owner, other = actors["owner"][0], actors["other"][0]
    first = loan_record(owner, movie["id"], copy_id=item["id"])
    second = loan_record(other, movie["id"], copy_id=item["id"])
    expect_abort(
        backend,
        lambda work: (work.circulation.insert_loan(first), work.circulation.insert_loan(second)),
    )
    assert backend.query(lambda work: work.circulation.loan(first["id"])) is None
    assert backend.query(lambda work: work.circulation.loan(second["id"])) is None
    pending = [loan_record(owner, movie["id"]) for _ in range(2)]
    expect_abort(backend, lambda work: [work.circulation.insert_loan(row) for row in pending])

    # Sequence uniqueness and waiting natural-key uniqueness are distinct constraints.
    def reservations(work, duplicate_sequence):
        records = [
            {
                "id": str(uuid.uuid4()),
                "movie_id": movie["id"],
                "user_id": user,
                "status": "waiting",
                "sequence": work.revision - index,
                "created_at": NOW,
                "expires_at": NOW + 10,
                "closed_at": None,
                "loan_id": None,
            }
            for index, user in enumerate((owner, other) if duplicate_sequence else (owner, owner))
        ]
        if duplicate_sequence:
            records[1]["sequence"] = records[0]["sequence"]
        for row in records:
            work.circulation.insert_reservation(row)

    expect_abort(backend, lambda work: reservations(work, True))
    expect_abort(backend, lambda work: reservations(work, False))

    # Touch six active loans for one borrower in a single UoW: every write must abort.
    actors, template = seed(backend)
    films, items, active = [], [], []
    for _ in range(6):
        film = {
            **template,
            "id": str(uuid.uuid4()),
            "title": "Active quota " + uuid.uuid4().hex[:12],
        }
        physical = {**item, "id": str(uuid.uuid4()), "movie_id": film["id"]}
        films.append(film)
        items.append(physical)
        active.append(loan_record(actors["owner"][0], film["id"], copy_id=physical["id"]))

    def six_active(work):
        for film, physical, row in zip(films, items, active, strict=True):
            work.catalogue.insert_movie(film)
            work.catalogue.insert_copy(physical)
            work.circulation.insert_loan(row)

    expect_abort(backend, six_active)
    assert all(
        backend.query(lambda work, identity=row["id"]: work.circulation.loan(identity)) is None
        for row in active
    )
    assert all(
        backend.query(lambda work, identity=row["id"]: work.catalogue.movie(identity)) is None
        for row in films
    )

    for kind in ("pending", "waiting"):
        actors, template = seed(backend)
        base = backend.query(lambda work: work.revision)
        films = [
            {**template, "id": str(uuid.uuid4()), "title": kind + uuid.uuid4().hex[:12]}
            for _ in range(11)
        ]
        for film in films:
            backend.run(lambda work, film=film: work.catalogue.insert_movie(film))
        records = [
            loan_record(actors["owner"][0], film["id"])
            if kind == "pending"
            else {
                "id": str(uuid.uuid4()),
                "movie_id": film["id"],
                "user_id": actors["owner"][0],
                "status": "waiting",
                "sequence": base + index + 1,
                "created_at": NOW,
                "expires_at": NOW + 10,
                "closed_at": None,
                "loan_id": None,
            }
            for index, film in enumerate(films)
        ]

        def eleven(work):
            method = (
                work.circulation.insert_loan
                if kind == "pending"
                else work.circulation.insert_reservation
            )
            for row in records:
                method(row)

        expect_abort(backend, eleven)
        assert (
            backend.query(lambda work: work.circulation.count_loans(actors["owner"][0], "pending"))
            == 0
        )
        assert (
            backend.query(lambda work: work.circulation.count_waiting_for_user(actors["owner"][0]))
            == 0
        )
    return {"native_or_json_constraints": "passed", "active_pending_waiting_limits": [5, 10, 10]}


class CirculationConstraintTests(unittest.TestCase):
    def test_final_uniqueness_parents_and_per_member_quotas_on_real_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.json"
            V2Store(path).initialize()
            result = exercise_circulation_constraints(JsonPersistence(path))
            self.assertEqual(result["native_or_json_constraints"], "passed")
