"""Security regressions for admission before anonymous password work."""

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from test_enterprise_auth import account
from test_enterprise_auth import enterprise as enterprise

from app import create_app

CONTEXT_KEY = b"synthetic-client-context-key-1234567890"
TIMESTAMP = 1700000000
PASSWORD = "synthetic admission passphrase"


@pytest.fixture
def proxied(enterprise):
    original, path = enterprise
    return create_app(
        {
            "TESTING": True,
            "DATA_FILE": original.application.config["DATA_FILE"],
            "ENTERPRISE_ENABLED": True,
            "ENTERPRISE_DATA_FILE": str(path),
            "AUTH_THROTTLE_KEY": b"x" * 32,
            "AUTH_CLOCK": lambda: TIMESTAMP,
            "CLIENT_CONTEXT_KEY": CONTEXT_KEY,
            "CLIENT_CONTEXT_REQUIRED": True,
        }
    ).test_client()


def signed_post(client, path, data, address="198.51.100.10", timestamp=TIMESTAMP, **changes):
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    message = "\n".join(
        (
            "cinevault-client-v1",
            str(timestamp),
            address,
            "POST",
            path,
            hashlib.sha256(encoded).hexdigest(),
        )
    )
    headers = {
        "Content-Type": "application/json",
        "X-CineVault-Client-IP": address,
        "X-CineVault-Client-Time": str(timestamp),
        "X-CineVault-Client-Signature": hmac.new(
            CONTEXT_KEY, message.encode("ascii"), hashlib.sha256
        ).hexdigest(),
    }
    headers.update(changes.get("headers", {}))
    return client.post(path, data=changes.get("body", encoded), headers=headers)


def registration(username):
    return {"username": username, "display_name": username, "password": PASSWORD}


def test_registration_duplicate_is_rejected_before_password_hashing(enterprise, monkeypatch):
    client, path = enterprise
    account(client, "existing-admission")
    calls = []

    def unexpected_hash(password):
        calls.append(True)
        raise AssertionError("Duplicate registration must not allocate an Argon2 computation")

    monkeypatch.setattr("app.enterprise_auth.hasher", SimpleNamespace(hash=unexpected_hash))
    response = client.post(
        "/api/v1/auth/register",
        json={
            "username": "existing-admission",
            "display_name": "Synthetic duplicate",
            "password": "synthetic admission passphrase",
        },
    )
    assert response.status_code == 409
    assert calls == []


def test_failures_from_one_proxied_visitor_do_not_block_another(proxied):
    for name in ("first-visitor", "second-visitor"):
        assert signed_post(proxied, "/api/v1/auth/register", registration(name)).status_code == 201
    for _ in range(5):
        assert (
            signed_post(
                proxied,
                "/api/v1/auth/login",
                {"username": "first-visitor", "password": "incorrect"},
            ).status_code
            == 401
        )
    response = signed_post(
        proxied,
        "/api/v1/auth/login",
        {"username": "second-visitor", "password": PASSWORD},
        address="198.51.100.11",
    )
    assert response.status_code == 200
    assert response.json["token_type"] == "Bearer"


@pytest.mark.parametrize("timestamp", [TIMESTAMP - 31, TIMESTAMP + 31])
def test_expired_or_future_client_assertion_is_rejected(proxied, timestamp):
    assert (
        signed_post(
            proxied, "/api/v1/auth/register", registration("time-visitor"), timestamp=timestamp
        ).status_code
        == 401
    )


def test_client_assertion_is_bound_to_body_and_address(proxied):
    data = registration("tampered-visitor")
    changed = json.dumps({**data, "display_name": "Changed"}).encode("utf-8")
    assert signed_post(proxied, "/api/v1/auth/register", data, body=changed).status_code == 401
    assert (
        signed_post(
            proxied,
            "/api/v1/auth/register",
            data,
            headers={"X-CineVault-Client-IP": "198.51.100.11"},
        ).status_code
        == 401
    )


def test_required_context_does_not_trust_forwarded_header(proxied):
    response = proxied.post(
        "/api/v1/auth/register",
        json=registration("unsigned-visitor"),
        headers={"X-Forwarded-For": "198.51.100.11"},
    )
    assert response.status_code == 401


def test_registration_budget_rejects_before_hashing(enterprise, monkeypatch):
    client, path = enterprise
    for index in range(20):
        assert (
            client.post("/api/v1/auth/register", json=registration(f"budget-{index}")).status_code
            == 201
        )
    calls = []

    def unexpected_hash(password):
        calls.append(True)
        raise AssertionError("Exhausted registration budget must reject before hashing")

    monkeypatch.setattr("app.enterprise_auth.hasher", SimpleNamespace(hash=unexpected_hash))
    response = client.post("/api/v1/auth/register", json=registration("budget-overflow"))
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert calls == []


def test_parallel_registration_does_not_allocate_a_second_hash(enterprise, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app import enterprise_auth

    client, path = enterprise
    original_hash = enterprise_auth.hasher.hash
    entered, release = Event(), Event()
    calls = []

    def controlled_hash(password):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5), "The test must release the first admitted hash"
        return original_hash(password)

    def first_registration():
        with client.application.test_client() as separate:
            return separate.post("/api/v1/auth/register", json=registration("parallel-first"))

    monkeypatch.setattr("app.enterprise_auth.hasher", SimpleNamespace(hash=controlled_hash))
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(first_registration)
        assert entered.wait(5)
        try:
            second = client.post("/api/v1/auth/register", json=registration("parallel-second"))
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 201
    assert second.status_code == 429
    assert calls == [True]


def test_required_context_preserves_automatic_options(proxied):
    response = proxied.options("/api/v1/auth/register")
    assert response.status_code == 200
    assert "POST" in response.headers["Allow"]


def registration_in_process(path, username, events, release=None):
    from pathlib import Path

    from app import enterprise_auth

    application = create_app(
        {
            "TESTING": True,
            "DATA_FILE": str(Path(path).parent / "legacy.json"),
            "ENTERPRISE_ENABLED": True,
            "ENTERPRISE_DATA_FILE": str(path),
            "AUTH_THROTTLE_KEY": b"x" * 32,
            "AUTH_CLOCK": lambda: TIMESTAMP,
        }
    )
    if release is not None:
        original = enterprise_auth.hasher.hash

        def controlled_hash(password):
            events.put("hash-started")
            assert release.wait(15), "Parent must release the admitted computation"
            return original(password)

        enterprise_auth.hasher = SimpleNamespace(hash=controlled_hash)
    response = application.test_client().post("/api/v1/auth/register", json=registration(username))
    events.put(response.status_code)


def test_two_processes_share_admission_without_holding_business_lock(enterprise):
    import multiprocessing

    import portalocker

    client, path = enterprise
    context = multiprocessing.get_context("spawn")
    first_events, second_events, release = context.Queue(), context.Queue(), context.Event()
    first = context.Process(
        target=registration_in_process,
        args=(path, "process-first", first_events, release),
    )
    second = context.Process(
        target=registration_in_process, args=(path, "process-second", second_events)
    )
    started = []
    try:
        first.start()
        started.append(first)
        assert first_events.get(timeout=10) == "hash-started"
        # The expensive operation may own its admission lock, never the business lock.
        with portalocker.Lock(
            str(path) + ".lock", timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB
        ):
            pass
        second.start()
        started.append(second)
        assert second_events.get(timeout=10) == 429
    finally:
        release.set()
        for process in started:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join()
                raise AssertionError("Admission process timed out")
    assert first_events.get(timeout=5) == 201
    assert all(process.exitcode == 0 for process in started)


def test_admission_expiry_and_corruption_preservation(tmp_path):
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from app.registration_admission import RegistrationAdmission, RegistrationBusy

    admission = RegistrationAdmission(tmp_path / "business.json")
    key = "a" * 64
    for _ in range(20):
        assert admission.compute(key, TIMESTAMP, lambda: "admitted") == "admitted"
    before = admission.path.read_bytes()
    with pytest.raises(RegistrationBusy):
        admission.compute(key, TIMESTAMP + 59, lambda: pytest.fail("Budget must reject"))
    assert admission.path.read_bytes() == before
    with pytest.raises(ValueError):
        admission.compute(key, TIMESTAMP - 1, lambda: pytest.fail("Clock rollback must reject"))
    assert admission.path.read_bytes() == before
    assert admission.compute(key, TIMESTAMP + 60, lambda: "renewed") == "renewed"
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "schemas/registration-admission.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(admission.path.read_bytes()))
    admission.path.write_bytes(b'{"invalid":true}')
    with pytest.raises(ValueError):
        admission.compute(key, TIMESTAMP + 120, lambda: pytest.fail("Corruption must reject"))
    assert admission.path.read_bytes() == b'{"invalid":true}'


@pytest.mark.parametrize("suffix", ["", ".lock"])
def test_metadata_cache_cannot_alias_admission_state_or_lock(enterprise, suffix):
    client, path = enterprise
    with pytest.raises(RuntimeError):
        create_app(
            {
                **client.application.config,
                "METADATA_ENABLED": True,
                "WIKIMEDIA_USER_AGENT": "CineVault/0.1 (https://example.test/contact)",
                "METADATA_CACHE_FILE": str(path) + ".registration.json" + suffix,
            }
        )


def test_registration_capacity_is_checked_before_hashing(enterprise, monkeypatch):
    import uuid

    from app.storage_v2 import V2Store

    client, path = enterprise
    assert (
        client.post("/api/v1/auth/register", json=registration("capacity-original")).status_code
        == 201
    )
    store = V2Store(path)

    def fill(document):
        template = document["users"][0]
        document["users"].extend(
            {
                **template,
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"capacity-{index}")),
                "username": f"capacity-{index}",
            }
            for index in range(9999)
        )

    store.transact(fill)
    before = path.read_bytes()

    def unexpected_hash(password):
        pytest.fail("Full account storage must reject before password hashing")

    monkeypatch.setattr("app.enterprise_auth.hasher", SimpleNamespace(hash=unexpected_hash))
    assert (
        client.post("/api/v1/auth/register", json=registration("capacity-overflow")).status_code
        == 503
    )
    assert path.read_bytes() == before


def test_corrupt_admission_fails_closed_without_hash_or_business_write(enterprise, monkeypatch):
    client, path = enterprise
    assert (
        client.post("/api/v1/auth/register", json=registration("before-corruption")).status_code
        == 201
    )
    admission = client.application.extensions["registration_admission"]
    admission.path.write_bytes(b'{"invalid":true}')
    before = path.read_bytes()

    def unexpected_hash(password):
        pytest.fail("Corrupt admission state must reject before password hashing")

    monkeypatch.setattr("app.enterprise_auth.hasher", SimpleNamespace(hash=unexpected_hash))
    response = client.post("/api/v1/auth/register", json=registration("after-corruption"))
    assert response.status_code == 503
    assert path.read_bytes() == before
    assert admission.path.read_bytes() == b'{"invalid":true}'
