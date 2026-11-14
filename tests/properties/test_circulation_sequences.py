"""Bounded generated plans against the common service and fresh real JSON files."""

import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from .circulation import (
    Ledger,
    expired_receipt_scenario,
    fifo_scenario,
    maintenance_scenario,
    quota_scenario,
    receipt_scenario,
)
from .harness import NOW, scenario_fixture
from .metrics import record


@settings(max_examples=25, deadline=None)
@given(
    st.permutations((0, 1, 3)),
    st.tuples(st.booleans(), st.booleans(), st.booleans()),
    st.sampled_from(("none", "before", "at", "after")),
)
def test_fifo_generated_plans(order, removals, expiry):
    cancelled = [actor for actor, remove in zip(order, removals, strict=True) if remove]
    record("circulation_fifo_json", fifo_scenario(order, cancelled, expiry))


@settings(max_examples=10, deadline=None)
@given(st.permutations(tuple(range(11))), st.integers(0, 4))
def test_quota_generated_plans(order, returned_slot):
    record("circulation_quotas_json", quota_scenario(order, returned_slot))


@settings(max_examples=6, deadline=None)
@given(st.sampled_from(("loan", "reservation")), st.sampled_from(("before", "at", "after")))
def test_receipt_generated_plans(kind, boundary):
    record("circulation_receipts_json", receipt_scenario(kind, boundary))


def test_fifo_tail_cannot_take_head_slot_regression():
    fifo_scenario((0, 1, 3), (), "none")


def test_expiration_exact_and_cancelled_history_regression():
    fifo_scenario((3, 0, 1), (3,), "at")


def test_historical_receipt_and_exact_ttl_regression():
    receipt_scenario("reservation", "at")


def test_pending_active_waiting_real_quota_regression():
    quota_scenario(tuple(range(11)), 2)


def test_maintenance_exception_vs_normal_refusal_regression():
    maintenance_scenario()


def test_expired_reservation_receipt_cannot_resurrect_history_regression():
    expired_receipt_scenario()


@pytest.mark.parametrize("kind", ["loan", "reservation"])
def test_oracle_detects_unplanned_valid_identity(kind):
    with scenario_fixture() as fixture:
        ledger = Ledger(fixture)
        ledger.create(kind, 0, fixture.movie["id"])
        method = "request_loan" if kind == "loan" else "create_reservation"
        injected = getattr(ledger.service, method)(
            ledger.token(1), {"movie_id": fixture.movie["id"]}, timestamp=NOW
        )
        assert injected.status == 201
        with pytest.raises(AssertionError, match="Unplanned circulation identity"):
            ledger.check()


@pytest.mark.parametrize("shared", [False, True])
def test_oracle_detects_unknown_audit_from_owned_actor(shared):
    with scenario_fixture() as outer:
        with scenario_fixture(outer.backend if shared else None) as fixture:
            ledger = Ledger(fixture)
            ledger.create("loan", 0, fixture.movie["id"])
            ledger.check()
            event = {
                "id": str(uuid.uuid4()),
                "actor_id": fixture.actors[0]["id"],
                "entity_type": "loan",
                "entity_id": str(uuid.uuid4()),
                "action": "loan.requested",
                "timestamp": NOW,
            }
            fixture.backend.run(lambda work: work.events.append(event))
            with pytest.raises(AssertionError, match="Exactly the planned circulation audits"):
                ledger.check()


def test_fresh_json_oracle_detects_unknown_system_audit():
    with scenario_fixture() as fixture:
        ledger = Ledger(fixture)
        ledger.create("loan", 0, fixture.movie["id"])
        event = {
            "id": str(uuid.uuid4()),
            "actor_id": None,
            "entity_type": "reservation",
            "entity_id": str(uuid.uuid4()),
            "action": "reservation.expired",
            "timestamp": NOW,
        }
        fixture.backend.run(lambda work: work.events.append(event))
        with pytest.raises(AssertionError, match="Exactly the planned circulation audits"):
            ledger.check()


@pytest.mark.parametrize("kind", ["loan", "reservation"])
def test_oracle_detects_pages_ignoring_actor_even_when_union_is_unchanged(monkeypatch, kind):
    class Port:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def loans_page(self, owner, query):
            return self.inner.loans_page(None, query)

        def reservations_page(self, owner, query):
            return self.inner.reservations_page(None, query)

    class Work:
        def __init__(self, inner):
            self.inner = inner
            self.circulation = Port(inner.circulation)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with scenario_fixture() as fixture:
        ledger = Ledger(fixture)
        ledger.create(kind, 0, fixture.movie["id"])
        ledger.create(kind, 1, fixture.movie["id"])
        ledger.check()
        real_query = fixture.backend.query
        monkeypatch.setattr(
            fixture.backend, "query", lambda callback: real_query(lambda work: callback(Work(work)))
        )
        with pytest.raises(AssertionError, match="Owned circulation page must match actor"):
            ledger.check()


@pytest.mark.parametrize("noise", [0, 125])
def test_shared_backend_ignores_foreign_system_expiry_with_complete_audit_pages(noise):
    with scenario_fixture() as foreign:
        other = Ledger(foreign)
        foreign_id, _ = other.create("reservation", 0, foreign.movie["id"])
        with scenario_fixture(foreign.backend) as fixture:
            assert fixture.path is None  # Same provided-backend mode as the native corpus.
            ledger = Ledger(fixture)
            ledger.create("loan", 0, fixture.movie["id"])
            ledger.jump(NOW + 1209600)
            ledger.call("reservations", ledger.token(2), [], status=200)
            actual = fixture.backend.query(lambda work: work.circulation.reservation(foreign_id))
            assert actual["status"] == "expired"
            # Retained foreign system events are legitimate historical references.
            events = [
                {
                    "id": str(uuid.uuid4()),
                    "actor_id": None,
                    "entity_type": "reservation",
                    "entity_id": str(uuid.uuid4()),
                    "action": "reservation.expired",
                    "timestamp": ledger.now,
                }
                for _ in range(noise)
            ]
            fixture.backend.run(lambda work: [work.events.append(row) for row in events])
            ledger.check()
