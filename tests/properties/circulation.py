"""Symbolic circulation plans, shared by JSON and an optional native controller.

The oracle records contractual transitions of named tickets, queue order and one
physical slot. It imports no production policy, ETag, receipt or expiry helper.
"""

import copy
import uuid

from app.circulation_model import CirculationQuery
from app.services.circulation import CirculationService
from app.validation import ValidationError

from .harness import NOW, scenario_fixture


class Ledger:
    def __init__(self, fixture):
        self.fixture = fixture
        self.service = CirculationService(fixture.backend)
        self.now = NOW
        self.steps = 0
        self.loans, self.reservations, self.versions = {}, {}, {}
        self.events = []
        self.sequence = 0

    def call(self, method, *args, status, message="Circulation outcome", **kwargs):
        self.steps += 1
        result = getattr(self.service, method)(*args, timestamp=self.now, **kwargs)
        assert result.status == status, message
        return result

    def token(self, actor):
        return self.fixture.actors[actor]["token"]

    def event(self, kind, identity, verb, actor):
        actor_id = None if actor is None else self.fixture.actors[actor]["id"]
        self.events.append((kind, identity, kind + "." + verb, actor_id, self.now))

    def create(self, kind, actor, movie, key=None):
        method = "request_loan" if kind == "loan" else "create_reservation"
        result = self.call(method, self.token(actor), {"movie_id": movie}, key=key, status=201)
        body = result.body
        identity = body["id"]
        expected = {"id": identity, "movie_id": movie}
        if kind == "loan":
            expected.update(
                borrower_id=self.fixture.actors[actor]["id"],
                copy_id=None,
                status="pending",
                requested_at=self.now,
                approved_at=None,
                due_at=None,
                returned_at=None,
                cancelled_at=None,
            )
            target = self.loans
        else:
            assert type(body["sequence"]) is int and body["sequence"] > self.sequence, (
                "Reservation order must be strictly increasing"
            )
            self.sequence = body["sequence"]  # Opaque ordinal; only relative order matters.
            expected.update(
                user_id=self.fixture.actors[actor]["id"],
                status="waiting",
                sequence=self.sequence,
                created_at=self.now,
                expires_at=self.now + 1209600,
                closed_at=None,
                loan_id=None,
            )
            target = self.reservations
        assert identity not in self.loans and identity not in self.reservations
        assert {k: v for k, v in body.items() if k != "_etag"} == expected
        assert body["_etag"] == result.headers["ETag"]
        target[identity] = expected
        self.versions[identity] = result.headers["ETag"]
        self.event(kind, identity, "requested" if kind == "loan" else "created", actor)
        return identity, copy.deepcopy(body)

    def cancel(self, kind, actor, identity):
        result = self.call(
            "cancel_loan" if kind == "loan" else "cancel_reservation",
            self.token(actor),
            identity,
            if_match=self.versions[identity],
            status=200,
        )
        target = self.loans if kind == "loan" else self.reservations
        target[identity].update(status="cancelled")
        target[identity]["cancelled_at" if kind == "loan" else "closed_at"] = self.now
        self.versions[identity] = result.headers["ETag"]
        self.event(kind, identity, "cancelled", actor)

    def approve(self, identity, item, reservation=None):
        result = self.call(
            "approve",
            self.token(2),
            identity,
            {"copy_id": item, "due_at": self.now + 3600},
            if_match=self.versions[identity],
            status=200,
        )
        self.loans[identity].update(
            status="active", copy_id=item, approved_at=self.now, due_at=self.now + 3600
        )
        self.versions[identity] = result.headers["ETag"]
        if reservation is not None:
            self.reservations[reservation].update(
                status="fulfilled", loan_id=identity, closed_at=self.now
            )
            self.event("reservation", reservation, "fulfilled", 2)
        self.event("loan", identity, "approved", 2)

    def returned(self, identity):
        result = self.call(
            "return_loan", self.token(2), identity, if_match=self.versions[identity], status=200
        )
        self.loans[identity].update(status="returned", returned_at=self.now)
        self.versions[identity] = result.headers["ETag"]
        self.event("loan", identity, "returned", 2)

    def expire(self, identities):
        for identity in identities:  # Plan's insertion order, never repository order.
            self.reservations[identity].update(status="expired", closed_at=self.now)
            self.event("reservation", identity, "expired", None)

    def check(self):
        def read(work):
            # The DTO only shapes bounded repository reads; no production
            # predicate computes the oracle's expected identities or states.
            query = CirculationQuery(limit=100)
            listed_loans, listed_reservations = {}, {}
            for actor in self.fixture.actors:
                for method, owner_field, expected_all, listed in (
                    (work.circulation.loans_page, "borrower_id", self.loans, listed_loans),
                    (
                        work.circulation.reservations_page,
                        "user_id",
                        self.reservations,
                        listed_reservations,
                    ),
                ):
                    rows, total = method(actor["id"], query)
                    identities = [row["id"] for row in rows]
                    assert set(identities) <= set(expected_all), "Unplanned circulation identity"
                    expected = {
                        identity: row
                        for identity, row in expected_all.items()
                        if row[owner_field] == actor["id"]
                    }
                    assert (
                        type(total) is int
                        and total == len(rows) == len(expected) < 100
                        and len(set(identities)) == len(identities)
                        and set(identities) == set(expected)
                        and all(row[owner_field] == actor["id"] for row in rows)
                    ), "Owned circulation page must match actor"
                    assert {row["id"]: row for row in rows} == expected, (
                        "Loan history and slot ownership must match the plan"
                        if owner_field == "borrower_id"
                        else "Reservation history must match the plan"
                    )
                    listed.update((row["id"], row) for row in rows)
            assert set(listed_loans) == set(self.loans), "Unplanned circulation identity: loan"
            assert set(listed_reservations) == set(self.reservations), (
                "Unplanned circulation identity: reservation"
            )
            loans = {identity: work.circulation.loan(identity) for identity in self.loans}
            reservations = {
                identity: work.circulation.reservation(identity) for identity in self.reservations
            }
            events, total = work.events.page(100, 0)
            assert type(total) is int and 0 <= total <= 2000 and len(events) == min(100, total), (
                "Bounded audit oracle page must be complete"
            )
            for offset in range(100, total, 100):
                batch, observed_total = work.events.page(100, offset)
                assert observed_total == total and len(batch) == min(100, total - offset), (
                    "Bounded audit oracle page must be complete"
                )
                events.extend(batch)
            assert len({event["id"] for event in events}) == total, (
                "Bounded audit oracle identities must be unique"
            )
            return loans, reservations, events

        loans, reservations, events = self.fixture.backend.query(read)
        assert loans == self.loans, "Loan history and slot ownership must match the plan"
        assert reservations == self.reservations, "Reservation history must match the plan"
        occupied = [row["copy_id"] for row in loans.values() if row["status"] == "active"]
        assert len(occupied) == len(set(occupied)), "One active loan per physical slot"
        identities = set(loans) | set(reservations)
        actors = {actor["id"] for actor in self.fixture.actors}
        isolated_json = self.fixture.path is not None
        actual = [
            (e["entity_type"], e["entity_id"], e["action"], e["actor_id"], e["timestamp"])
            for e in events
            if isolated_json or e["actor_id"] in actors or e["entity_id"] in identities
        ]
        assert actual == list(reversed(self.events)), "Exactly the planned circulation audits"

    def jump(self, now):
        self.now = now
        self.fixture.renew(now)  # New valid sessions, not fictitious long-lived login sessions.


def movie_and_copy(fixture, *, with_copy=True):
    movie = {**fixture.movie, "id": str(uuid.uuid4()), "title": "Circulation property"}
    item = {
        "id": str(uuid.uuid4()),
        "movie_id": movie["id"],
        "support": "dvd",
        "location": "P4",
        "condition": "good",
    }

    def seed(work):
        work.catalogue.insert_movie(movie)
        if with_copy:
            work.catalogue.insert_copy(item)

    fixture.backend.run(seed)
    return movie["id"], item["id"] if with_copy else None


def fifo_scenario(order, cancelled, expiry, *, backend=None):
    """One occupied slot; permuted tickets, cancellations and exact expiry cases."""
    with scenario_fixture(backend) as fixture:
        ledger = Ledger(fixture)
        movie, item = movie_and_copy(fixture)
        holder, _ = ledger.create("loan", 2, movie)
        ledger.approve(holder, item)
        loans, reservations, receipts = {}, {}, {}
        for actor in order:
            loans[actor], receipts[actor] = ledger.create(
                "loan", actor, movie, "circulation-loan-replay"
            )
            reservations[actor], _ = ledger.create("reservation", actor, movie)
            ledger.check()
        for actor in cancelled:
            ledger.cancel("reservation", actor, reservations[actor])
            ledger.check()
        queued = [actor for actor in order if actor not in cancelled]
        # Explicit boundary table: before is waiting, at/after is expired.
        if expiry != "none":
            ledger.jump(NOW + {"before": 1209599, "at": 1209600, "after": 1209601}[expiry])
            ledger.call("reservations", ledger.token(2), [], status=200)
            if expiry in {"at", "after"}:
                ledger.expire([reservations[actor] for actor in queued])
                queued = []
            ledger.check()
        ledger.returned(holder)
        ledger.check()
        schedule = queued + [actor for actor in reversed(order) if actor not in queued]
        for index, actor in enumerate(schedule):
            if actor in queued and queued.index(actor) + 1 < len(queued):
                tail = queued[-1]
                options = ledger.call("options", ledger.token(2), loans[tail], status=200)
                assert options.body["copies"] == [], "FIFO tail options must stay empty"
                result = ledger.call(
                    "approve",
                    ledger.token(2),
                    loans[tail],
                    {"copy_id": item, "due_at": ledger.now + 3600},
                    if_match=ledger.versions[loans[tail]],
                    status=409,
                    message="FIFO tail cannot pass a waiting head",
                )
                assert result.error["code"] == "reservation_priority"
                ledger.check()
            options = ledger.call("options", ledger.token(2), loans[actor], status=200)
            assert [row["id"] for row in options.body["copies"]] == [item], (
                "Queue head must receive the newly free slot"
            )
            ledger.approve(loans[actor], item, reservations[actor] if actor in queued else None)
            if index + 1 < len(schedule):
                next_actor = schedule[index + 1]
                result = ledger.call(
                    "approve",
                    ledger.token(2),
                    loans[next_actor],
                    {"copy_id": item, "due_at": ledger.now + 3600},
                    if_match=ledger.versions[loans[next_actor]],
                    status=409,
                    message="Last occupied copy cannot be attributed twice",
                )
                assert result.error["code"] == "copy_unavailable"
            if expiry == "none":
                result = ledger.call(
                    "request_loan",
                    ledger.token(actor),
                    {"movie_id": movie},
                    key="circulation-loan-replay",
                    status=201,
                )
                assert result.body == receipts[actor], "Receipt must remain the historical response"
                assert result.headers["Idempotency-Replayed"] == "true"
            ledger.check()
            ledger.returned(loans[actor])
            if expiry == "none":
                result = ledger.call(
                    "request_loan",
                    ledger.token(actor),
                    {"movie_id": movie},
                    key="circulation-loan-replay",
                    status=201,
                )
                assert result.body == receipts[actor], "Returned loan replay cannot reopen history"
            ledger.check()
        return ledger.steps


def receipt_scenario(kind, boundary, *, backend=None):
    """Close the live entity, replay history, cross operation and actor scope, TTL."""
    with scenario_fixture(backend) as fixture:
        ledger = Ledger(fixture)
        movie, _ = movie_and_copy(fixture, with_copy=False)
        method = "request_loan" if kind == "loan" else "create_reservation"
        other_method = "create_reservation" if kind == "loan" else "request_loan"
        key = "circulation-receipt-shared-key"
        identity, historical = ledger.create(kind, 0, movie, key)
        ledger.create(kind, 1, movie, key)
        result = ledger.call(
            other_method, ledger.token(0), {"movie_id": movie}, key=key, status=409
        )
        assert result.error["code"] == "operation_conflict", "Receipt namespace is actor-key"
        other_movie, _ = movie_and_copy(fixture, with_copy=False)
        conflict = ledger.call(
            method, ledger.token(0), {"movie_id": other_movie}, key=key, status=409
        )
        assert conflict.error["code"] == "operation_conflict", "Receipt content conflict"
        ledger.cancel(kind, 0, identity)
        result = ledger.call(method, ledger.token(0), {"movie_id": movie}, key=key, status=201)
        assert result.body == historical, "Closed entity replay must remain historical"
        assert result.headers["Idempotency-Replayed"] == "true"
        ledger.check()
        ledger.jump(NOW + {"before": 86399, "at": 86400, "after": 86401}[boundary])
        if boundary == "before":
            result = ledger.call(method, ledger.token(0), {"movie_id": movie}, key=key, status=201)
            assert result.body == historical and result.headers["Idempotency-Replayed"] == "true"
        else:
            new_identity, new_body = ledger.create(kind, 0, movie, key)
            assert new_identity != identity, "Expired receipt cannot resurrect the closed identity"
            result = ledger.call(method, ledger.token(0), {"movie_id": movie}, key=key, status=201)
            assert result.body == new_body and result.headers["Idempotency-Replayed"] == "true"
        ledger.check()
        return ledger.steps


def quota_scenario(order, returned_slot, *, backend=None):
    """Exercise actual ten pending, five active and ten waiting thresholds."""
    with scenario_fixture(backend) as fixture:
        ledger = Ledger(fixture)
        films = [movie_and_copy(fixture) for _ in range(11)]
        loans = []
        for position in order[:10]:
            loan, _ = ledger.create("loan", 0, films[position][0])
            loans.append(loan)
        final_movie = films[order[10]][0]
        result = ledger.call("request_loan", ledger.token(0), {"movie_id": final_movie}, status=409)
        assert result.error["code"] == "loan_limit", "Ten pending loans must be enforced"
        ledger.cancel("loan", 0, loans[9])
        ledger.create("loan", 0, final_movie)
        for index in range(5):
            ledger.approve(loans[index], films[order[index]][1])
        result = ledger.call(
            "approve",
            ledger.token(2),
            loans[5],
            {"copy_id": films[order[5]][1], "due_at": ledger.now + 3600},
            if_match=ledger.versions[loans[5]],
            status=409,
        )
        assert result.error["code"] == "loan_limit", "Five active loans must be enforced"
        ledger.check()
        ledger.returned(loans[returned_slot])
        ledger.approve(loans[5], films[order[5]][1])
        ledger.check()
        waiting_movies = [movie_and_copy(fixture, with_copy=False)[0] for _ in range(11)]
        waiting = [ledger.create("reservation", 1, waiting_movies[i])[0] for i in order[:10]]
        result = ledger.call(
            "create_reservation",
            ledger.token(1),
            {"movie_id": waiting_movies[order[10]]},
            status=409,
        )
        assert result.error["code"] == "reservation_limit", "Ten waiting tickets must be enforced"
        ledger.cancel("reservation", 1, waiting[returned_slot])
        ledger.create("reservation", 1, waiting_movies[order[10]])
        ledger.check()
        return ledger.steps


def maintenance_scenario(*, backend=None):
    """Payload exceptions roll back expiry; a normal refusal commits maintenance."""
    with scenario_fixture(backend) as fixture:
        ledger = Ledger(fixture)
        movie, _ = movie_and_copy(fixture, with_copy=False)
        identities = [ledger.create("reservation", actor, movie)[0] for actor in (0, 1, 3)]
        ledger.jump(NOW + 1209600)
        before = fixture.backend.query(lambda work: work.revision)
        try:
            ledger.service.create_reservation(ledger.token(0), {}, timestamp=ledger.now)
        except ValidationError:
            pass
        else:
            raise AssertionError("Invalid payload must raise and roll back maintenance")
        assert fixture.backend.query(lambda work: work.revision) == before
        ledger.check()
        ledger.call("reservations", ledger.token(0), [("scope", "all")], status=403)
        ledger.expire(identities)
        ledger.check()
        return ledger.steps + 1


def expired_receipt_scenario(*, backend=None):
    """Fourteen-day expiry necessarily outlives the twenty-four-hour receipt."""
    with scenario_fixture(backend) as fixture:
        ledger = Ledger(fixture)
        movie, _ = movie_and_copy(fixture, with_copy=False)
        key = "circulation-expired-reservation-key"
        old, _ = ledger.create("reservation", 0, movie, key)
        ledger.jump(NOW + 1209600)
        ledger.expire([old])
        new, _ = ledger.create("reservation", 0, movie, key)
        assert new != old, "Expired reservation key must allocate a new identity"
        ledger.check()
        return ledger.steps
