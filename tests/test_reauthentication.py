import hashlib
import hmac
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_enterprise_auth import account
from test_enterprise_auth import enterprise as enterprise

from app import create_app
from app.storage_v2 import V2Store

ENDPOINT = "/api/v1/me/password"
PASSWORD = "test passphrase 123"
NEW_PASSWORD = "new passphrase 456"
TIMESTAMP = 10000


def reauth_key(identity):
    return hmac.new(b"x" * 32, ("reauth:user:" + identity).encode(), hashlib.sha256).hexdigest()


def change(client, auth, password="wrong", new_password=NEW_PASSWORD, **kwargs):
    return client.put(
        ENDPOINT,
        headers=auth,
        json={"current_password": password, "new_password": new_password},
        **kwargs,
    )


def context(enterprise):
    client, path = enterprise
    client.application.config["AUTH_CLOCK"] = lambda: TIMESTAMP
    user, auth = account(client)
    return client, path, user, auth


def seed_attempt(path, identity, count, window_start=TIMESTAMP):
    V2Store(path).transact(
        lambda doc: doc["auth_attempts"].append(
            {"key": reauth_key(identity), "window_start": window_start, "count": count}
        )
    )


def test_five_failed_reauths_block_the_sixth_before_password_verification(enterprise, monkeypatch):
    from app import enterprise_auth

    client, path, user, auth = context(enterprise)
    for _ in range(5):
        assert change(client, auth).status_code == 401
    original = V2Store(path).read()["users"][0]["password_hash"]

    def unexpected_verify(*args):
        raise AssertionError("Blocked reauthentication must not invoke Argon2")

    monkeypatch.setattr(
        enterprise_auth,
        "hasher",
        SimpleNamespace(verify=unexpected_verify, hash=enterprise_auth.hasher.hash),
    )
    for password in ("wrong", PASSWORD):
        response = change(client, auth, password)
        assert response.status_code == 429
        assert response.json["error"]["code"] == "rate_limited"
        assert response.headers["Retry-After"] == "900"
        assert response.headers["Cache-Control"] == "no-store"
    document = V2Store(path).read()
    assert document["users"][0]["password_hash"] == original
    assert document["auth_attempts"] == [
        {"key": reauth_key(user["id"]), "window_start": TIMESTAMP, "count": 5}
    ]
    assert client.get("/api/v1/me", headers=auth).status_code == 200


def test_reauth_budget_is_shared_by_sessions_addresses_and_restart(enterprise):
    client, path, user, auth = context(enterprise)
    token = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json["token"]
    other_session = {"Authorization": "Bearer " + token}
    _, other_user = account(client, "otheraccount")
    seed_attempt(path, user["id"], 4)
    assert change(client, auth, environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code == 401
    restarted = create_app(dict(client.application.config)).test_client()
    assert (
        change(restarted, other_session, environ_overrides={"REMOTE_ADDR": "192.0.2.2"}).status_code
        == 429
    )
    assert change(restarted, other_user).status_code == 401


@pytest.mark.parametrize("elapsed,expected", [(899, 429), (900, 401)])
def test_reauth_window_expires_at_nine_hundred_seconds(enterprise, elapsed, expected):
    client, path, user, auth = context(enterprise)
    seed_attempt(path, user["id"], 5)
    client.application.config["AUTH_CLOCK"] = lambda: TIMESTAMP + elapsed
    assert change(client, auth).status_code == expected
    attempt = V2Store(path).read()["auth_attempts"][0]
    assert attempt["count"] == (5 if elapsed == 899 else 1)
    assert attempt["window_start"] == (TIMESTAMP if elapsed == 899 else TIMESTAMP + 900)


def test_login_cannot_erase_a_future_reauth_window(enterprise):
    client, path, user, auth = context(enterprise)
    seed_attempt(path, user["id"], 5, TIMESTAMP + 10)
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 200
    assert change(client, auth).status_code == 429
    assert V2Store(path).read()["auth_attempts"] == [
        {"key": reauth_key(user["id"]), "window_start": TIMESTAMP + 10, "count": 5}
    ]


def test_login_cannot_erase_a_future_login_window(enterprise):
    client, path, user, auth = context(enterprise)
    key = hmac.new(b"x" * 32, b"user:alice", hashlib.sha256).hexdigest()
    V2Store(path).transact(
        lambda doc: doc["auth_attempts"].append(
            {"key": key, "window_start": TIMESTAMP + 10, "count": 5}
        )
    )
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 429
    assert V2Store(path).read()["auth_attempts"][0]["count"] == 5


def test_full_attempt_storage_fails_closed_without_verifying_or_eviction(enterprise, monkeypatch):
    from app import enterprise_auth

    client, path, user, auth = context(enterprise)
    attempts = [
        {"key": f"{index:064x}", "window_start": TIMESTAMP, "count": 1} for index in range(10000)
    ]
    V2Store(path).transact(lambda doc: doc["auth_attempts"].extend(attempts))

    def unexpected_verify(*args):
        raise AssertionError("No verification is admitted without persistent attempt capacity")

    monkeypatch.setattr(
        enterprise_auth,
        "hasher",
        SimpleNamespace(verify=unexpected_verify, hash=enterprise_auth.hasher.hash),
    )
    response = change(client, auth)
    assert response.status_code == 503
    assert response.json["error"]["code"] == "auth_attempt_capacity"
    assert V2Store(path).read()["auth_attempts"] == attempts


def test_existing_reauth_counter_can_use_the_last_attempt_at_full_capacity(enterprise):
    client, path, user, auth = context(enterprise)
    seed_attempt(path, user["id"], 4)
    V2Store(path).transact(
        lambda doc: doc["auth_attempts"].extend(
            {"key": f"{index:064x}", "window_start": TIMESTAMP, "count": 1} for index in range(9999)
        )
    )
    assert change(client, auth).status_code == 401
    assert change(client, auth).status_code == 429
    assert len(V2Store(path).read()["auth_attempts"]) == 10000


def test_login_reserves_both_missing_counters_before_verification(enterprise, monkeypatch):
    from app import enterprise_auth

    client, path, user, auth = context(enterprise)
    attempts = [
        {"key": f"{index:064x}", "window_start": TIMESTAMP, "count": 1} for index in range(9999)
    ]
    V2Store(path).transact(lambda doc: doc["auth_attempts"].extend(attempts))

    def unexpected_verify(*args):
        raise AssertionError("Both login counter slots must be available before Argon2")

    monkeypatch.setattr(
        enterprise_auth,
        "hasher",
        SimpleNamespace(verify=unexpected_verify, hash=enterprise_auth.hasher.hash),
    )
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})
    assert response.status_code == 503
    assert response.json["error"]["code"] == "auth_attempt_capacity"
    assert V2Store(path).read()["auth_attempts"] == attempts


def test_expired_attempts_release_storage_for_reauthentication(enterprise):
    client, path, user, auth = context(enterprise)
    V2Store(path).transact(
        lambda doc: doc["auth_attempts"].extend(
            {"key": f"{index:064x}", "window_start": TIMESTAMP - 900, "count": 1}
            for index in range(10000)
        )
    )
    assert change(client, auth).status_code == 401
    assert V2Store(path).read()["auth_attempts"] == [
        {"key": reauth_key(user["id"]), "window_start": TIMESTAMP, "count": 1}
    ]


def test_invalid_new_password_does_not_spend_a_reauth_attempt(enterprise):
    client, path, user, auth = context(enterprise)
    assert change(client, auth, new_password="short").status_code == 422
    assert V2Store(path).read()["auth_attempts"] == []
    seed_attempt(path, user["id"], 4)
    assert change(client, auth, PASSWORD).status_code == 204
    assert client.get("/api/v1/me", headers=auth).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": NEW_PASSWORD}
        ).status_code
        == 200
    )


def reauth_in_process(path, authorization, ready, release, results):
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
    ready.put(True)
    if not release.wait(15):
        raise AssertionError("Parent must release concurrent reauthentication")
    results.put(change(application.test_client(), authorization).status_code)


def test_two_processes_share_the_last_reauth_attempt(enterprise):
    client, path, user, auth = context(enterprise)
    seed_attempt(path, user["id"], 4)
    processes_context = multiprocessing.get_context("spawn")
    ready, results, release = (
        processes_context.Queue(),
        processes_context.Queue(),
        processes_context.Event(),
    )
    processes = [
        processes_context.Process(
            target=reauth_in_process, args=(path, auth, ready, release, results)
        )
        for _ in range(2)
    ]
    started = []
    try:
        for process in processes:
            process.start()
            started.append(process)
        for _ in processes:
            assert ready.get(timeout=15) is True
        release.set()
        for process in started:
            process.join(20)
            assert not process.is_alive(), "Reauthentication process timed out"
            assert process.exitcode == 0
        assert sorted(results.get(timeout=5) for _ in processes) == [401, 429]
    finally:
        release.set()
        for process in started:
            if process.is_alive():
                process.terminate()
            process.join(5)
    assert V2Store(path).read()["auth_attempts"] == [
        {"key": reauth_key(user["id"]), "window_start": TIMESTAMP, "count": 5}
    ]
