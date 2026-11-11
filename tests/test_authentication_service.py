"""Pure policy tests and a real-driver/Argon2 contract callable without pytest."""

import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store
from app.validation import ValidationError

PASSWORD = "current passphrase 123"
NEW_PASSWORD = "new passphrase 456"
TIMESTAMP = 10000
TOKEN = "existing_" + "x" * 32


def implementation():
    assert importlib.util.find_spec("app.services") is not None, (
        "The pure authentication service is not implemented"
    )
    from app.services.authentication import AuthenticationService

    return AuthenticationService


def user(username="member", password_hash="$argon2id$fixture"):
    return {
        "id": str(uuid.uuid4()),
        "username": username,
        "display_name": "Membre",
        "password_hash": password_hash,
        "role": "member",
        "enabled": True,
        "created_at": 0,
    }


class ProbeHasher:
    """Cheap crypto seam for policy ordering, never evidence of password security."""

    def __init__(self):
        self.calls = []

    def verify(self, encoded, supplied):
        self.calls.append(("verify", encoded))
        expected = NEW_PASSWORD if encoded == "$argon2id$new-fixture" else PASSWORD
        if encoded == "$argon2id$dummy" or supplied != expected:
            raise VerifyMismatchError("Invalid fixture secret")
        return True

    def hash(self, supplied):
        self.calls.append(("hash", None))
        return "$argon2id$new-fixture"


def configure(backend, crypto):
    return implementation()(
        backend, hasher=crypto, dummy_hash="$argon2id$dummy", throttle_key=b"x" * 32
    )


class AuthenticationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "enterprise.json"
        V2Store(self.path).initialize()
        self.backend = JsonPersistence(self.path)
        self.account = user()
        self.backend.run(lambda work: work.accounts.insert(self.account))
        self.crypto = ProbeHasher()

    def service(self):
        return configure(self.backend, self.crypto)

    def login(self):
        return self.service().login("member", PASSWORD, address="192.0.2.1", timestamp=TIMESTAMP)

    def seed_session(self, token=TOKEN, **changes):
        from app.services.authentication import token_digest

        record = {
            "id": str(uuid.uuid4()),
            "user_id": self.account["id"],
            "token_hash": token_digest(token),
            "created_at": TIMESTAMP,
            "last_seen": TIMESTAMP,
            "expires_at": TIMESTAMP + 28800,
            **changes,
        }
        self.backend.run(lambda work: work.sessions.insert(record))
        return record

    def test_service_profile_and_logout_use_pure_results(self):
        service = self.service()
        login = service.login("member", PASSWORD, address="192.0.2.1", timestamp=TIMESTAMP)
        self.assertEqual(login.status, 200)
        token = login.body["token"]
        self.assertNotIn(token, repr(login))
        self.assertNotIn(token.encode(), self.path.read_bytes())
        self.assertEqual(service.me(token, timestamp=TIMESTAMP).body["username"], "member")
        changed = service.update_me(token, "Nouveau nom", timestamp=TIMESTAMP)
        self.assertEqual(changed.body["display_name"], "Nouveau nom")
        self.assertEqual(service.logout(token, timestamp=TIMESTAMP).status, 204)
        before = self.path.read_bytes()
        self.assertEqual(service.me(token, timestamp=TIMESTAMP).status, 401)
        self.assertEqual(self.path.read_bytes(), before)

    def test_fifth_failure_blocks_before_crypto_and_preserves_future_budgets(self):
        from app.services.authentication import attempt_key

        service = self.service()
        self.seed_session()
        for _ in range(5):
            self.assertEqual(
                service.change_password(TOKEN, "wrong", NEW_PASSWORD, timestamp=TIMESTAMP).status,
                401,
            )
        calls = len(self.crypto.calls)
        result = service.change_password(TOKEN, PASSWORD, NEW_PASSWORD, timestamp=TIMESTAMP)
        self.assertEqual(result.status, 429)
        self.assertEqual(result.headers["Retry-After"], "900")
        self.assertEqual(len(self.crypto.calls), calls)
        key = attempt_key(b"x" * 32, "reauth:user:", self.account["id"])
        V2Store(self.path).transact(
            lambda doc: doc["auth_attempts"][0].update(window_start=TIMESTAMP + 50)
        )
        self.assertEqual(self.login().status, 200)
        record = self.backend.query(lambda work: work.attempts.get_many([key]))[key]
        self.assertEqual((record["window_start"], record["count"]), (TIMESTAMP + 50, 5))

    def test_success_hashes_after_current_verification_and_revokes_all_sessions(self):
        self.seed_session()
        self.seed_session("second_" + "z" * 32)
        service = self.service()
        self.assertEqual(
            service.change_password(TOKEN, PASSWORD, NEW_PASSWORD, timestamp=TIMESTAMP).status,
            204,
        )
        self.assertEqual(self.crypto.calls, [("verify", "$argon2id$fixture"), ("hash", None)])
        self.assertEqual(V2Store(self.path).read()["sessions"], [])
        self.assertEqual(self.login().status, 401)
        self.assertEqual(
            service.login("member", NEW_PASSWORD, address="192.0.2.1", timestamp=TIMESTAMP).status,
            200,
        )

    def test_capacity_reserves_both_login_keys_before_any_verification(self):
        service = self.service()
        records = [
            {"key": f"{i:064x}", "window_start": TIMESTAMP + 1, "count": 1} for i in range(9999)
        ]
        V2Store(self.path).transact(lambda doc: doc["auth_attempts"].extend(records))
        result = service.login("member", "wrong", address="192.0.2.1", timestamp=TIMESTAMP)
        self.assertEqual(result.status, 503)
        self.assertEqual(result.error["code"], "auth_attempt_capacity")
        self.assertEqual(self.crypto.calls, [])
        self.assertEqual(V2Store(self.path).read()["auth_attempts"], records)

    def test_expired_attempts_release_capacity_at_exact_window_boundary(self):
        service = self.service()
        old = [
            {"key": f"{i:064x}", "window_start": TIMESTAMP - 900, "count": 5} for i in range(10000)
        ]
        V2Store(self.path).transact(lambda doc: doc["auth_attempts"].extend(old))
        self.assertEqual(
            service.login("member", "wrong", address="192.0.2.1", timestamp=TIMESTAMP).status, 401
        )
        attempts = V2Store(self.path).read()["auth_attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(
            all(item["count"] == 1 and item["window_start"] == TIMESTAMP for item in attempts)
        )

    def test_session_expiry_idle_and_clock_rollback_abort_without_rewrite(self):
        service = self.service()
        self.seed_session()
        for timestamp in (TIMESTAMP - 1, TIMESTAMP + 1800, TIMESTAMP + 28800):
            before = self.path.read_bytes()
            self.assertEqual(service.me(TOKEN, timestamp=timestamp).status, 401)
            self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_profile_or_new_password_keeps_validation_before_authentication(self):
        service = self.service()
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            service.update_me("invalid", " leading", timestamp=TIMESTAMP)
        with self.assertRaises(ValidationError):
            service.change_password("invalid", "wrong", "short", timestamp=TIMESTAMP)
        self.assertEqual(
            service.change_password("invalid", {}, NEW_PASSWORD, timestamp=TIMESTAMP).status, 401
        )
        self.assertEqual(self.crypto.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_or_invalid_username_uses_dummy_and_no_selector_reaches_repository(self):
        service = self.service()
        for username in ("unknown", {"$ne": None}):
            result = service.login(username, "wrong", address="192.0.2.1", timestamp=TIMESTAMP)
            self.assertEqual(result.status, 401)
        self.assertEqual(self.crypto.calls, [("verify", "$argon2id$dummy")] * 2)

    def test_login_retry_retains_one_prepared_token_and_session_identity(self):
        from app.services.authentication import token_digest

        recorded = []
        backend = self.backend

        class InjectedAbort(Exception):
            pass

        class RetryFirstCommand:
            def run(self, command):
                def invoke(work, *, abort=False):
                    result = command(work)
                    recorded.append(
                        (
                            result.body["token"],
                            work.sessions.by_token_hash(token_digest(result.body["token"])),
                        )
                    )
                    if abort:
                        raise InjectedAbort()
                    return result

                try:
                    backend.run(lambda work: invoke(work, abort=True))
                except InjectedAbort:
                    pass
                return backend.run(invoke)

        result = configure(RetryFirstCommand(), self.crypto).login(
            "member", PASSWORD, address="192.0.2.1", timestamp=TIMESTAMP
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded[0], recorded[1])
        self.assertEqual(V2Store(self.path).read()["sessions"], [recorded[1][1]])


def test_http_auth_core_uses_pure_results_and_current_crypto(tmp_path, monkeypatch):
    from app import create_app, enterprise_auth
    from app.services.results import ServiceResult

    path = tmp_path / "enterprise.json"
    V2Store(path).initialize()
    app = create_app(
        {
            "TESTING": True,
            "DATA_FILE": str(tmp_path / "legacy.json"),
            "ENTERPRISE_ENABLED": True,
            "ENTERPRISE_DATA_FILE": str(path),
            "AUTH_THROTTLE_KEY": b"x" * 32,
            "AUTH_CLOCK": lambda: TIMESTAMP,
        }
    )
    persistence = app.extensions.get("enterprise_persistence")
    assert isinstance(persistence, JsonPersistence)
    crypto = ProbeHasher()
    monkeypatch.setattr(enterprise_auth, "hasher", crypto)
    persistence.run(lambda work: work.accounts.insert(user()))
    original_run = persistence.run
    outcomes = []

    def observed(command):
        def pure(work):
            result = command(work)
            assert isinstance(result, ServiceResult)
            outcomes.append(result.status)
            return result

        return original_run(pure)

    monkeypatch.setattr(persistence, "run", observed)
    client = app.test_client()
    login = client.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD})
    assert login.status_code == 200
    auth = {"Authorization": "Bearer " + login.json["token"]}
    assert client.get("/api/v1/me", headers=auth).status_code == 200
    assert (
        client.patch("/api/v1/me", headers=auth, json={"display_name": "Nouveau"}).status_code
        == 200
    )
    assert (
        client.put(
            "/api/v1/me/password",
            headers=auth,
            json={
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
            },
        ).status_code
        == 204
    )
    login = client.post("/api/v1/auth/login", json={"username": "member", "password": NEW_PASSWORD})
    assert login.status_code == 200
    auth = {"Authorization": "Bearer " + login.json["token"]}
    assert client.post("/api/v1/auth/logout", headers=auth).status_code == 204
    assert outcomes == [200, 200, 200, 204, 200, 204]
    assert crypto.calls == [
        ("verify", "$argon2id$fixture"),
        ("verify", "$argon2id$fixture"),
        ("hash", None),
        ("verify", "$argon2id$new-fixture"),
    ]


def exercise_auth_service(database):
    """Real MongoDB + real Argon2; caller provisions a NEW disposable database."""
    from app.persistence.mongo import MongoPersistence
    from app.services.authentication import AuthenticationService, attempt_key

    backend = MongoPersistence(database)
    crypto = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    account = user("authprobe_" + uuid.uuid4().hex[:8], crypto.hash(PASSWORD))
    backend.run(lambda work: work.accounts.insert(account))
    service = AuthenticationService(
        backend, hasher=crypto, dummy_hash=crypto.hash("dummy passphrase"), throttle_key=b"x" * 32
    )
    result = service.login(account["username"], PASSWORD, address="192.0.2.1", timestamp=TIMESTAMP)
    assert result.status == 200
    token = result.body["token"]
    second = service.login(account["username"], PASSWORD, address="192.0.2.1", timestamp=TIMESTAMP)
    assert service.me(token, timestamp=TIMESTAMP).body["id"] == account["id"]
    assert (
        service.update_me(token, "Nom modifié", timestamp=TIMESTAMP).body["display_name"]
        == "Nom modifié"
    )
    for _ in range(5):
        assert (
            service.change_password(token, "wrong", NEW_PASSWORD, timestamp=TIMESTAMP).status == 401
        )
    limited = service.change_password(token, PASSWORD, NEW_PASSWORD, timestamp=TIMESTAMP)
    assert limited.status == 429 and limited.headers["Retry-After"] == "900"
    key = attempt_key(b"x" * 32, "reauth:user:", account["id"])
    backend.run(
        lambda work: work.attempts.set({"key": key, "window_start": TIMESTAMP + 20, "count": 5})
    )
    assert (
        service.login(
            account["username"], PASSWORD, address="192.0.2.2", timestamp=TIMESTAMP
        ).status
        == 200
    )
    assert (
        backend.query(lambda work: work.attempts.get_many([key]))[key]["window_start"]
        == TIMESTAMP + 20
    )
    before = backend.query(lambda work: work.revision)
    assert service.me(token, timestamp=TIMESTAMP - 1).status == 401
    assert backend.query(lambda work: work.revision) == before
    changed_at = TIMESTAMP + 920
    assert (
        service.change_password(token, PASSWORD, NEW_PASSWORD, timestamp=changed_at).status == 204
    )
    assert service.me(second.body["token"], timestamp=changed_at).status == 401
    new_login = service.login(
        account["username"], NEW_PASSWORD, address="192.0.2.1", timestamp=changed_at
    )
    assert new_login.status == 200
    assert service.logout(new_login.body["token"], timestamp=changed_at).status == 204
    assert service.me(new_login.body["token"], timestamp=changed_at).status == 401
    assert database.entity_ids.count_documents({"kind": "auth_attempts"}) == 0
    return {
        "real_auth_service": "passed",
        "reauth_failures_checked": 5,
        "future_window_preserved": True,
    }


if __name__ == "__main__":
    unittest.main()
