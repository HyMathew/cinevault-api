"""Identity policy over real JSON units of work; reusable backend contract."""

import importlib.util
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.circulation_model import PayloadError, RequestBody
from app.persistence.json import JsonPersistence
from app.registration_admission import RegistrationAdmission, RegistrationBusy
from app.services.authentication import token_digest
from app.storage_v2 import V2Store
from app.validation import ValidationError

NOW = 1000


def implementation():
    assert importlib.util.find_spec("app.services.accounts") is not None, (
        "Registration and admin services must use repository ports"
    )
    from app.services.accounts import AdminAccountService, RegistrationService

    return RegistrationService, AdminAccountService


def account(name, role="member"):
    return {
        "id": str(uuid.uuid4()),
        "username": name,
        "display_name": name,
        "role": role,
        "enabled": True,
        "created_at": NOW,
        "password_hash": "$argon2id$fixture",
    }


def seed_account(backend, name, role="member"):
    user = account(name, role)
    token = uuid.uuid4().hex * 2
    session = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "token_hash": token_digest(token),
        "created_at": NOW,
        "last_seen": NOW,
        "expires_at": NOW + 28800,
    }
    backend.run(lambda work: (work.accounts.insert(user), work.sessions.insert(session)))
    return user, token, session


@pytest.fixture
def backend(tmp_path):
    path = tmp_path / "accounts.json"
    V2Store(path).initialize()
    return JsonPersistence(path), path


def exercise_directory_filters(persistence):
    _, admin = implementation()
    directory = admin(persistence)
    unique = uuid.uuid4().hex[:8]
    prefix = "cine." + unique
    _, operator, _ = seed_account(persistence, "ops-" + unique, "admin")
    _, member, _ = seed_account(persistence, "member-" + unique)
    data = [
        account(f"{prefix}-{number:03d}", "manager" if number >= 3 else "member")
        for number in range(112)
    ]
    data[65]["enabled"] = False
    distractor = account(f"cinex{unique}-050", "manager")
    persistence.run(lambda work: [work.accounts.insert(row) for row in [*data, distractor]])

    filters = [("prefix", prefix.upper()), ("role", "manager"), ("enabled", "true")]
    assert directory.users(member, filters, timestamp=NOW).status == 403
    assert directory.users("invalid", filters, timestamp=NOW).status == 401
    page = directory.users(operator, [*filters, ("limit", "50"), ("offset", "50")], timestamp=NOW)
    expected = [row["username"] for row in data[3:] if row["enabled"]]
    assert page.status == 200 and page.body["total"] == len(expected) == 108
    assert [row["username"] for row in page.body["users"]] == expected[50:100]
    assert all(
        set(row) == {"id", "username", "display_name", "role", "enabled"}
        for row in page.body["users"]
    )
    disabled = directory.users(operator, [*filters[:2], ("enabled", "false")], timestamp=NOW)
    assert disabled.body["total"] == 1 and disabled.body["users"][0]["id"] == data[65]["id"]
    empty = directory.users(operator, [("prefix", "absent-" + unique)], timestamp=NOW)
    assert empty.body["total"] == 0 and empty.body["users"] == []
    for invalid in (("prefix", "cine*"), ("prefix", "K"), ("role", "owner"), ("enabled", "1")):
        with pytest.raises(ValidationError):
            directory.users(operator, [invalid], timestamp=NOW)
    return {"directory_filters": "passed", "filtered_total": len(expected), "second_page": 50}


def exercise_native_directory_filters(database):
    from app.persistence.mongo import MongoPersistence

    return exercise_directory_filters(MongoPersistence(database))


def test_directory_filters_on_json(backend):
    persistence, _ = backend
    assert exercise_directory_filters(persistence)["directory_filters"] == "passed"


def prepared_hash(password):
    assert password == "long passphrase 123"
    return "$argon2id$prepared"


def exercise_account_commands(backend):
    registration, admin_service = implementation()
    service = registration(backend)
    name = "registered-" + uuid.uuid4().hex[:12]
    result = service.register(
        name.upper(), "Nom", "long passphrase 123", timestamp=NOW, prepare_hash=prepared_hash
    )
    assert result.status == 201
    assert result.body == {
        "id": result.body["id"],
        "username": name,
        "display_name": "Nom",
        "role": "member",
        "enabled": True,
    }
    stored = backend.query(lambda work: work.accounts.get(result.body["id"]))
    assert stored["password_hash"] == "$argon2id$prepared" and stored["created_at"] == NOW
    actor, token, _ = seed_account(backend, "admin-" + uuid.uuid4().hex[:12], "admin")
    admin = admin_service(backend)
    page = admin.users(token, [("limit", "1"), ("offset", "0")], timestamp=NOW)
    assert page.status == 200 and len(page.body["users"]) == 1
    assert page.body["total"] >= 2
    assert "password_hash" not in page.body["users"][0]
    assert admin.get(token, result.body["id"], timestamp=NOW).body == result.body
    assert admin.update(token, result.body["id"], {"role": "manager"}, timestamp=NOW).status == 200
    assert admin.get(token, result.body["id"], timestamp=NOW).body["role"] == "manager"
    return {"registration_and_admin": True, "actor": actor["id"]}


def test_shared_account_contract_uses_real_json(backend):
    exercise_account_commands(backend[0])


def test_account_count_is_consistent_with_page_and_read_only_scope(backend):
    store, _ = backend
    seed_account(store, "member")
    assert store.query(lambda work: (work.accounts.count(), len(work.accounts.page(100, 0)))) == (
        1,
        1,
    )


def test_duplicate_or_invalid_registration_rejects_before_preparation(backend):
    store, path = backend
    seed_account(store, "duplicate")
    registration, _ = implementation()

    def forbidden_hash(password):
        pytest.fail("Rejected registration must not allocate password work")

    before = path.read_bytes()
    service = registration(store)
    assert (
        service.register(
            "DUPLICATE", "Nom", "long passphrase 123", timestamp=NOW, prepare_hash=forbidden_hash
        ).status
        == 409
    )
    with pytest.raises(ValidationError):
        service.register("bad!", "Nom", "short", timestamp=NOW, prepare_hash=forbidden_hash)
    assert path.read_bytes() == before


def test_registration_rechecks_duplicate_after_external_preparation(backend):
    store, path = backend
    registration, _ = implementation()

    def concurrent_registration(password):
        # Runs outside the store lock: a competing writer can complete here.
        seed_account(JsonPersistence(path), "racer")
        return prepared_hash(password)

    result = registration(store).register(
        "racer",
        "Nom",
        "long passphrase 123",
        timestamp=NOW,
        prepare_hash=concurrent_registration,
    )
    assert result.status == 409
    assert store.query(lambda work: work.accounts.count()) == 1


def test_registration_capacity_is_rechecked_after_preparation_without_partial_account(backend):
    store, path = backend
    registration, _ = implementation()

    def fill(password):
        def mutation(document):
            document["users"] = [account(f"capacity-{i}") for i in range(10000)]

        V2Store(path).transact(mutation)
        return prepared_hash(password)

    result = registration(store).register(
        "new-member", "Nom", "long passphrase 123", timestamp=NOW, prepare_hash=fill
    )
    assert (result.status, result.error["code"]) == (503, "registration_capacity")
    assert store.query(lambda work: work.accounts.count()) == 10000
    assert store.query(lambda work: work.accounts.by_username("new-member")) is None


def test_registration_admission_failure_never_writes_business_state(backend):
    store, path = backend
    registration, _ = implementation()
    admission = RegistrationAdmission(path)
    before = path.read_bytes()

    def limited(password):
        raise RegistrationBusy(7)

    with pytest.raises(RegistrationBusy):
        registration(store).register(
            "member", "Nom", "long passphrase 123", timestamp=NOW, prepare_hash=limited
        )
    assert path.read_bytes() == before

    # Real admission, consumed before crypto and independent of the business lock.
    def admitted(password):
        return admission.compute("a" * 64, NOW, lambda: prepared_hash(password))

    assert (
        registration(store)
        .register("member", "Nom", "long passphrase 123", timestamp=NOW, prepare_hash=admitted)
        .status
        == 201
    )
    assert admission.read(NOW)["total"] == 1


def test_registration_preparation_and_identity_are_stable_when_command_is_retried(backend):
    store, path = backend
    registration, _ = implementation()
    ids, hashes = [], []

    class Abort(Exception):
        pass

    class Retry:
        query = store.query

        def run(self, command):
            def first(work):
                result = command(work)
                ids.append(result.body["id"])
                raise Abort()

            with pytest.raises(Abort):
                store.run(first)
            assert V2Store(path).read()["users"] == []
            result = store.run(command)
            ids.append(result.body["id"])
            return result

    def prepare(password):
        hashes.append(password)
        return prepared_hash(password)

    result = registration(Retry()).register(
        "member", "Nom", "long passphrase 123", timestamp=NOW, prepare_hash=prepare
    )
    assert ids == [result.body["id"], result.body["id"]]
    assert hashes == ["long passphrase 123"]
    assert store.query(lambda work: work.accounts.count()) == 1


def test_admin_authorization_precedes_query_payload_and_target_lookup(backend):
    store, path = backend
    _, admin_service = implementation()
    member, token, _ = seed_account(store, "member")
    admin = admin_service(store)
    before = path.read_bytes()
    assert admin.update("invalid", "bad-uuid", RequestBody(b"", False), timestamp=NOW).status == 401
    assert path.read_bytes() == before
    assert admin.users(token, [("limit", "bad")], timestamp=NOW).status == 403
    assert admin.update(token, "bad-uuid", RequestBody(b"", False), timestamp=NOW).status == 403
    assert admin.get(token, "bad-uuid", timestamp=NOW).status == 403
    assert admin.get(token, member["id"], timestamp=NOW).body["id"] == member["id"]


@pytest.mark.parametrize(
    "body,status",
    [
        (RequestBody(b"", False), 415),
        (RequestBody(b"{", True), 400),
        (RequestBody(b"", True, error=413), 413),
    ],
)
def test_admin_payload_failure_rolls_back_session_before_missing_target(backend, body, status):
    store, path = backend
    _, admin_service = implementation()
    _, token, _ = seed_account(store, "admin", "admin")
    before = path.read_bytes()
    with pytest.raises(PayloadError) as exc:
        admin_service(store).update(token, "bad-uuid", body, timestamp=NOW + 1)
    assert exc.value.status == status
    assert path.read_bytes() == before


def test_admin_last_admin_and_revocation_are_atomic(backend):
    store, _ = backend
    _, admin_service = implementation()
    actor, token, _ = seed_account(store, "admin", "admin")
    member, member_token, _ = seed_account(store, "member")
    admin = admin_service(store)
    result = admin.update(token, actor["id"], {"role": "member"}, timestamp=NOW)
    assert (result.status, result.error["code"]) == (409, "last_admin")
    assert admin.update(token, member["id"], {"role": "manager"}, timestamp=NOW).status == 200
    assert store.query(lambda work: work.sessions.by_token_hash(token_digest(member_token))) is None
    assert admin.update(token, "bad-uuid", {"enabled": True}, timestamp=NOW).status == 404
    with pytest.raises(ValidationError):
        admin.update(token, actor["id"], {"enabled": 0}, timestamp=NOW)


def test_concurrent_admin_services_cannot_remove_last_admin(backend):
    store, path = backend
    _, admin_service = implementation()
    first, token_a, _ = seed_account(store, "admin-a", "admin")
    second, token_b, _ = seed_account(store, "admin-b", "admin")

    def demote(item):
        user, token = item
        return (
            admin_service(JsonPersistence(path))
            .update(token, user["id"], {"role": "member"}, timestamp=NOW)
            .status
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, [(first, token_a), (second, token_b)]))
    assert sorted(outcomes) == [200, 409]
    assert sum(u["role"] == "admin" and u["enabled"] for u in V2Store(path).read()["users"]) == 1


def test_two_registration_commands_recheck_the_same_username(backend):
    _, path = backend
    registration, _ = implementation()
    both_prepared = Barrier(2)

    def register(_):
        def prepare(password):
            both_prepared.wait(timeout=10)
            return prepared_hash(password)

        return (
            registration(JsonPersistence(path))
            .register("racer", "Nom", "long passphrase 123", timestamp=NOW, prepare_hash=prepare)
            .status
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(register, range(2)))
    assert sorted(outcomes) == [201, 409]
    assert len(V2Store(path).read()["users"]) == 1


@pytest.mark.parametrize(
    "point",
    ["accounts.save", "circulation.save_reservation", "events.append", "sessions.revoke_user"],
)
def test_disabling_account_rolls_back_all_real_writes_on_failure(backend, point):
    from test_circulation_atomicity import FailurePersistence, InjectedFailure
    from test_circulation_service import seed

    from app.services.circulation import CirculationService

    store, path = backend
    _, admin_service = implementation()
    actors, movie = seed(store)
    circulation = CirculationService(store)
    reservation = circulation.create_reservation(
        actors["owner"][1], {"movie_id": movie["id"]}, timestamp=NOW
    )
    loan = circulation.request_loan(actors["owner"][1], {"movie_id": movie["id"]}, timestamp=NOW)
    assert reservation.status == 201 and loan.status == 201
    before = path.read_bytes()
    with pytest.raises(InjectedFailure):
        admin_service(FailurePersistence(store, point)).update(
            actors["admin"][1], actors["owner"][0], {"enabled": False}, timestamp=NOW + 1
        )
    assert path.read_bytes() == before
    result = admin_service(store).update(
        actors["admin"][1], actors["owner"][0], {"enabled": False}, timestamp=NOW + 1
    )
    assert result.status == 200 and result.body["enabled"] is False
    document = V2Store(path).read()
    assert (
        next(row for row in document["reservations"] if row["id"] == reservation.body["id"])[
            "status"
        ]
        == "cancelled"
    )
    assert (
        next(row for row in document["loans"] if row["id"] == loan.body["id"])["status"]
        == "pending"
    )
    assert not any(row["user_id"] == actors["owner"][0] for row in document["sessions"])
    event = document["audit_events"][-1]
    assert (event["action"], event["entity_id"], event["actor_id"]) == (
        "reservation.cancelled",
        reservation.body["id"],
        actors["admin"][0],
    )


def test_admin_disable_retries_keep_the_same_audit_id(backend):
    from test_circulation_service import seed

    from app.services.circulation import CirculationService

    store, path = backend
    _, admin_service = implementation()
    actors, movie = seed(store)
    reservation = CirculationService(store).create_reservation(
        actors["owner"][1], {"movie_id": movie["id"]}, timestamp=NOW
    )
    assert reservation.status == 201
    events = []

    class Abort(Exception):
        pass

    class Retry:
        def run(self, command):
            def abort(work):
                command(work)
                events.append(work.events.page(1, 0)[0][0])
                raise Abort()

            with pytest.raises(Abort):
                store.run(abort)
            return store.run(command)

    result = admin_service(Retry()).update(
        actors["admin"][1], actors["owner"][0], {"enabled": False}, timestamp=NOW + 1
    )
    assert result.status == 200
    final = V2Store(path).read()["audit_events"][-1]
    assert final == events[0] and final["entity_id"] == reservation.body["id"]


def test_mongo_account_count_uses_only_users_and_transaction_session():
    from app.persistence.mongo_repositories import MongoAccounts

    calls = []

    class Users:
        def count_documents(self, selector, **kwargs):
            calls.append((selector, kwargs))
            return 17

    session = object()
    scope = SimpleNamespace(
        check=lambda: None,
        collection=lambda name: Users() if name == "users" else pytest.fail("Non-account read"),
        _session=session,
    )
    assert MongoAccounts(scope).count() == 17
    assert calls == [({}, {"session": session})]
