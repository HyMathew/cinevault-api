"""Fail after real writes, not instead of them, and verify the entire UoW aborts."""

import tempfile
import unittest
from pathlib import Path

from test_circulation_service import NOW, implementation, lending_fixture, seed

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store


class InjectedFailure(Exception):
    pass


class AfterRepository:
    def __init__(self, repository, method):
        self.repository, self.method = repository, method

    def __getattr__(self, name):
        operation = getattr(self.repository, name)
        if name != self.method:
            return operation

        def fail_after(*args, **kwargs):
            operation(*args, **kwargs)
            raise InjectedFailure()

        return fail_after


class AfterWork:
    def __init__(self, work, point):
        self.work, self.repository, self.method = work, *point.split(".")

    def __getattr__(self, name):
        value = getattr(self.work, name)
        return AfterRepository(value, self.method) if name == self.repository else value


class FailurePersistence:
    def __init__(self, backend, point):
        self.backend, self.point = backend, point

    def run(self, command):
        return self.backend.run(lambda work: command(AfterWork(work, self.point)))


def exercise_circulation_atomicity(backend, *, registry_count=None):
    service = implementation()(backend)
    actors, movie, item = lending_fixture(backend)
    owner, other, admin = (actors[name][1] for name in ("owner", "other", "admin"))
    body = {"movie_id": movie["id"]}
    initial = service.request_loan(owner, body, timestamp=NOW)
    approval = {"copy_id": item["id"], "due_at": NOW + 60}
    active = service.approve(
        admin, initial.body["id"], approval, if_match=initial.headers["ETag"], timestamp=NOW
    )
    reserved = service.create_reservation(other, body, timestamp=NOW)
    service.return_loan(admin, initial.body["id"], if_match=active.headers["ETag"], timestamp=NOW)
    pending = service.request_loan(other, body, timestamp=NOW)
    original_loan = backend.query(lambda work: work.circulation.loan(pending.body["id"]))
    original_reservation = backend.query(
        lambda work: work.circulation.reservation(reserved.body["id"])
    )
    for point in ("circulation.save_loan", "circulation.save_reservation", "events.append"):
        before = backend.query(lambda work: work.revision)
        original_events = backend.query(lambda work: work.events.page(100, 0))
        identities = registry_count() if registry_count is not None else None
        try:
            implementation()(FailurePersistence(backend, point)).approve(
                admin,
                pending.body["id"],
                approval,
                if_match=pending.headers["ETag"],
                timestamp=NOW,
            )
        except InjectedFailure:
            pass
        else:
            raise AssertionError("Approval failure injection did not execute")
        assert backend.query(lambda work: work.revision) == before
        assert (
            backend.query(lambda work: work.circulation.loan(pending.body["id"])) == original_loan
        )
        assert (
            backend.query(lambda work: work.circulation.reservation(reserved.body["id"]))
            == original_reservation
        )
        assert backend.query(lambda work: work.events.page(100, 0)) == original_events
        if registry_count is not None:
            assert registry_count() == identities

    for kind in ("loan", "reservation"):
        actors, movie = seed(backend)
        token, actor = actors["owner"][1], actors["owner"][0]
        for point in ("circulation.insert_" + kind, "events.append", "receipts.insert"):
            before = backend.query(lambda work: work.revision)
            original_events = backend.query(lambda work: work.events.page(100, 0))
            identities = registry_count() if registry_count is not None else None
            faulty = implementation()(FailurePersistence(backend, point))
            create = faulty.request_loan if kind == "loan" else faulty.create_reservation
            try:
                create(
                    token,
                    {"movie_id": movie["id"]},
                    key="atomicity-circulation-key-001",
                    timestamp=NOW,
                )
            except InjectedFailure:
                pass
            else:
                raise AssertionError("Creation failure injection did not execute")
            assert backend.query(lambda work: work.revision) == before
            assert (
                backend.query(lambda work: work.circulation.open_loan(actor, movie["id"])) is None
            )
            assert backend.query(lambda work: work.circulation.count_waiting_for_user(actor)) == 0
            assert backend.query(lambda work: work.events.page(100, 0)) == original_events
            if registry_count is not None:
                assert registry_count() == identities
    return {
        "real_write_then_abort_points": 9,
        "entry_link_audit_receipt_registry_rollback": "passed",
    }


class CirculationAtomicityTests(unittest.TestCase):
    def test_real_writes_then_faults_leave_no_partial_loan_reservation_or_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.json"
            V2Store(path).initialize()
            result = exercise_circulation_atomicity(JsonPersistence(path))
            self.assertEqual(result["real_write_then_abort_points"], 9)
