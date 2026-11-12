"""Named regressions used to inspect critical mutant detection, without a score."""

import uuid

from app.services.authentication import AuthenticationService
from app.services.personal import PersonalCollectionService
from app.services.receipts import cached_problem

from .harness import NOW, private_scenario, receipt_scenario, scenario_fixture


def test_private_model_ownership_etag_and_audit_regression():
    private_scenario([(7, 0, 0, "A"), (4, 0, 0, "A")] * 8)


def test_receipt_model_replay_conflict_delete_and_ttl_regression():
    receipt_scenario([(0, 0, 0, "A")] * 12)


def test_preflight_is_readonly_even_when_authority_is_valid():
    with scenario_fixture() as fixture:
        actor = fixture.actors[0]
        before = fixture.path.read_bytes()
        revision = fixture.backend.query(lambda work: work.revision)
        result = PersonalCollectionService(fixture.backend).authorize(
            actor["token"], timestamp=NOW + 1
        )
        assert result.status == 204
        assert fixture.path.read_bytes() == before, "Preflight committed state"
        assert fixture.backend.query(lambda work: work.revision) == revision


def test_absolute_expiry_idle_and_clock_rollback_are_separate_boundaries():
    for boundary in ("absolute", "idle", "rollback", "disabled"):
        with scenario_fixture() as fixture:
            actor = fixture.actors[0]
            now = NOW
            if boundary == "absolute":
                session = fixture.session_state(actor)
                fixture.backend.run(lambda work: work.sessions.touch(session["id"], NOW + 28799))
                now = NOW + 28800
            elif boundary == "idle":
                now = NOW + 1800
            elif boundary == "rollback":
                now = NOW - 1
            else:
                account = fixture.backend.query(lambda work: work.accounts.get(actor["id"]))
                fixture.backend.run(lambda work: work.accounts.save({**account, "enabled": False}))
            before = fixture.path.read_bytes()
            result = PersonalCollectionService(fixture.backend).authorize(
                actor["token"], timestamp=now
            )
            assert result.status == 401, "Session boundary: " + boundary
            assert fixture.path.read_bytes() == before


def test_sixth_failed_login_is_refused_before_the_crypto_provider():
    class RejectingProvider:
        calls = 0

        def verify(self, encoded, supplied):
            self.calls += 1
            return False

    with scenario_fixture() as fixture:
        provider = RejectingProvider()
        account = fixture.backend.query(lambda work: work.accounts.get(fixture.actors[0]["id"]))
        service = AuthenticationService(
            fixture.backend, hasher=provider, dummy_hash="synthetic", throttle_key=b"x" * 32
        )
        for _ in range(5):
            assert (
                service.login(
                    account["username"], "synthetic-invalid", address="127.0.0.1", timestamp=NOW
                ).status
                == 401
            )
        assert provider.calls == 5
        try:
            result = service.login(
                account["username"], "synthetic-invalid", address="127.0.0.1", timestamp=NOW
            )
        finally:
            assert provider.calls == 5, "Throttled request reached crypto provider"
        assert result.status == 429, "Sixth attempt must be throttled"
        assert provider.calls == 5, "Throttled request reached crypto provider"


def test_receipt_future_clock_and_cross_operation_are_rejected():
    cached = {"created_at": NOW + 1, "operation": "copy.create", "request_hash": "a" * 64}
    result = cached_problem(cached, "copy.create", "a" * 64, NOW)
    assert result is not None and result.error["code"] == "clock_state", (
        "Future receipt must be rejected"
    )
    result = cached_problem(cached, "catalog.import", "a" * 64, NOW + 1)
    assert result is not None and result.error["code"] == "operation_conflict", (
        "Cross-operation key must be rejected"
    )
    result = cached_problem(cached, "copy.create", "b" * 64, NOW + 1)
    assert result is not None and result.error["code"] == "operation_conflict", (
        "Receipt content conflict"
    )
    assert cached_problem(cached, "copy.create", "a" * 64, NOW + 1) is None


def test_audit_retains_the_last_append_even_when_its_clock_is_older():
    with scenario_fixture() as fixture:
        actor = fixture.actors[0]

        def append(work, identity, timestamp):
            work.events.append(
                {
                    "id": identity,
                    "actor_id": actor["id"],
                    "entity_id": fixture.movie["id"],
                    "entity_type": "movie",
                    "action": "movie.updated",
                    "timestamp": timestamp,
                }
            )

        fixture.backend.run(
            lambda work: [append(work, str(uuid.uuid4()), NOW + i) for i in range(2000)]
        )
        identity = str(uuid.uuid4())
        fixture.backend.run(lambda work: append(work, identity, NOW - 1))
        events, total = fixture.backend.query(lambda work: work.events.page(1, 0))
        assert total == 2000
        assert events[0]["id"] == identity, "Audit must retain append suffix, not oldest prefix"
