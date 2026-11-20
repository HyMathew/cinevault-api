"""Persistent operator account operations and their retry boundaries."""

import getpass
import sys
import warnings
from types import SimpleNamespace
from unittest.mock import Mock, call
from uuid import uuid4

import pytest

from app import account_cli, bootstrap, recovery
from app.persistence.errors import CommitOutcomeUnknown
from app.persistence.json import JsonPersistence
from app.services import accounts
from app.storage_v2 import V2Store

PASSWORD = "synthetic operator test passphrase"
REPLACEMENT_HASH = "$argon2id$synthetic-prepared"


def user(username, role="member", *, enabled=True):
    return {
        "id": str(uuid4()),
        "username": username,
        "display_name": username,
        "password_hash": "$argon2id$synthetic-existing",
        "role": role,
        "enabled": enabled,
        "created_at": 10,
    }


def session(account, digest):
    return {
        "id": str(uuid4()),
        "user_id": account["id"],
        "token_hash": digest * 64,
        "created_at": 10,
        "last_seen": 18,
        "expires_at": 100,
    }


@pytest.fixture
def store(tmp_path):
    store = V2Store(tmp_path / "accounts.json")
    store.initialize()
    return store


@pytest.mark.parametrize("blocked", ("disabled_admin", "taken_name", "capacity"))
def test_bootstrap_rejects_existing_constraints_before_argon(store, monkeypatch, blocked):
    existing = {
        "disabled_admin": user("oldadmin", "admin", enabled=False),
        "taken_name": user("firstadmin"),
        "capacity": user("oldmember"),
    }[blocked]
    store.transact(lambda doc: doc["users"].append(existing))
    if blocked == "capacity":
        monkeypatch.setattr(accounts, "ACCOUNT_CAPACITY", 1)
    before = store.path.read_bytes()

    def unexpected(_):
        pytest.fail("Rejected bootstrap must not run Argon2")

    monkeypatch.setattr(bootstrap, "hasher", SimpleNamespace(hash=unexpected))
    with pytest.raises(ValueError):
        bootstrap.bootstrap_admin(
            JsonPersistence(store.path), "firstadmin", "First Admin", PASSWORD
        )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("raced", ("disabled_admin", "taken_name", "capacity"))
def test_bootstrap_rechecks_constraints_after_argon(store, monkeypatch, raced):
    winner = {
        "disabled_admin": user("raceadmin", "admin", enabled=False),
        "taken_name": user("firstadmin"),
        "capacity": user("racemember"),
    }[raced]
    if raced == "capacity":
        monkeypatch.setattr(accounts, "ACCOUNT_CAPACITY", 1)

    def prepare_hash(value):
        assert value == PASSWORD
        store.transact(lambda doc: doc["users"].append(winner))
        return REPLACEMENT_HASH

    monkeypatch.setattr(bootstrap, "hasher", SimpleNamespace(hash=prepare_hash))
    with pytest.raises(ValueError):
        bootstrap.bootstrap_admin(
            JsonPersistence(store.path), "firstadmin", "First Admin", PASSWORD
        )
    assert store.read()["users"] == [winner]
    assert store.read()["revision"] == 1


def test_reset_rechecks_active_operator_after_argon(store, monkeypatch):
    admin, target = user("firstadmin", "admin"), user("targetuser", "manager")
    store.transact(lambda doc: doc["users"].extend((admin, target)))

    def prepare_hash(value):
        assert value == PASSWORD
        store.transact(lambda doc: doc["users"][0].update(enabled=False))
        return REPLACEMENT_HASH

    monkeypatch.setattr(recovery, "hasher", SimpleNamespace(hash=prepare_hash))
    with pytest.raises(ValueError, match="operator"):
        recovery.reset_password(
            JsonPersistence(store.path), "firstadmin", "targetuser", PASSWORD, timestamp=20
        )
    document = store.read()
    assert document["users"][1] == target
    assert document["audit_events"] == []
    assert document["revision"] == 2


def test_reset_preserves_clock_guard_and_retries_atomic_revoke_and_audit(store, monkeypatch):
    admin, target = user("firstadmin", "admin"), user("targetuser", "manager", enabled=False)
    admin_session, target_session = session(admin, "a"), session(target, "b")

    def seed(doc):
        doc["users"].extend((admin, target))
        doc["sessions"].extend((admin_session, target_session))

    store.transact(seed)
    prepared = []

    def prepare_hash(value):
        prepared.append(value)
        return REPLACEMENT_HASH

    monkeypatch.setattr(recovery, "hasher", SimpleNamespace(hash=prepare_hash))
    backend = JsonPersistence(store.path)
    original_run = backend.run
    attempted_events = []

    class RetryBody(Exception):
        pass

    def replay_once(operation):
        def abort_first(work):
            operation(work)
            attempted_events.append(work.events.page(1, 0)[0][0])
            raise RetryBody()

        with pytest.raises(RetryBody):
            original_run(abort_first)
        assert store.read()["sessions"] == [admin_session, target_session]
        assert store.read()["audit_events"] == []
        return original_run(operation)

    monkeypatch.setattr(backend, "run", replay_once)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="clock"):
        recovery.reset_password(backend, "firstadmin", "targetuser", PASSWORD, timestamp=17)
    assert not prepared
    assert store.path.read_bytes() == before
    recovery.reset_password(
        backend, " FIRSTADMIN ", "targetuser", PASSWORD, timestamp=20, enable=True
    )
    document = store.read()
    assert prepared == [PASSWORD]
    assert document["users"] == [
        admin,
        {**target, "enabled": True, "password_hash": REPLACEMENT_HASH},
    ]
    assert document["sessions"] == [admin_session]
    assert document["audit_events"] == attempted_events
    assert attempted_events[0] == {
        "id": attempted_events[0]["id"],
        "actor_id": admin["id"],
        "entity_id": target["id"],
        "entity_type": "recovery",
        "action": "recovery.password_reset",
        "timestamp": 20,
    }
    assert document["revision"] == 2


@pytest.mark.parametrize(
    ("module", "arguments"),
    (
        (bootstrap, ["--storage-backend", "mongodb", "--store", "mixed.json"]),
        (
            recovery,
            [
                "reset-password",
                "--operator",
                "admin",
                "--username",
                "member",
                "--mongo-uri-file",
                "unselected.secret",
                "--mongo-database",
                "cinevault",
            ],
        ),
        (
            recovery,
            [
                "restore",
                "--snapshot",
                "source.json",
                "--target",
                "new.json",
                "--sha256",
                "0" * 64,
                "--storage-backend",
                "mongodb",
            ],
        ),
    ),
)
def test_cli_refuses_ambiguous_or_unsupported_backends(module, arguments, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["accounts", *arguments])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert "interactive terminal" not in capsys.readouterr().err


def test_cli_opens_explicit_mongo_backend_and_closes_even_if_commit_is_uncertain(
    store, monkeypatch, capsys
):
    backend = JsonPersistence(store)
    backend.close = Mock()
    factory = Mock(return_value=backend)
    monkeypatch.setitem(
        sys.modules, "app.persistence.factory", SimpleNamespace(open_mongo_persistence=factory)
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(names))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: PASSWORD)
    monkeypatch.setattr(bootstrap, "hasher", SimpleNamespace(hash=lambda value: REPLACEMENT_HASH))
    monkeypatch.setattr(recovery, "hasher", SimpleNamespace(hash=lambda value: REPLACEMENT_HASH))

    names = iter(("firstadmin", "First Admin"))
    selection = [
        "--storage-backend",
        "mongodb",
        "--mongo-uri-file",
        "private/mongodb.uri",
        "--mongo-database",
        "cinevault_test",
    ]
    monkeypatch.setattr(sys, "argv", ["bootstrap", *selection])
    bootstrap.main()
    assert "Administrator created" in capsys.readouterr().out

    monkeypatch.setattr(backend, "run", Mock(side_effect=CommitOutcomeUnknown("private detail")))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recovery",
            "reset-password",
            *selection,
            "--operator",
            "firstadmin",
            "--username",
            "firstadmin",
        ],
    )
    with pytest.raises(SystemExit) as error:
        recovery.main()
    captured = capsys.readouterr()
    assert error.value.code == 1
    assert "uncertain" in captured.err and "private detail" not in captured.err
    assert PASSWORD not in captured.out + captured.err
    expected = {
        "STORAGE_BACKEND": "mongodb",
        "ENTERPRISE_ENABLED": True,
        "MONGODB_URI_FILE": "private/mongodb.uri",
        "MONGODB_DATABASE": "cinevault_test",
    }
    assert factory.call_args_list == [call(expected), call(expected)]
    assert backend.close.call_count == 2


def test_cli_does_not_echo_rejected_password_argument(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--store", "local.json", "--password", PASSWORD])
    with pytest.raises(SystemExit) as error:
        bootstrap.main()
    assert error.value.code == 2
    assert PASSWORD not in capsys.readouterr().err


def test_cli_cancels_if_getpass_cannot_hide_input(monkeypatch):
    def fail_without_echo(prompt):
        warnings.warn("fallback", getpass.GetPassWarning, stacklevel=1)
        pytest.fail("Input must not continue on an echoing terminal")

    monkeypatch.setattr(getpass, "getpass", fail_without_echo)
    with pytest.raises(OSError, match="hidden input"):
        account_cli.hidden_input("Password: ")


def test_mongo_operator_reads_use_owned_session_and_exclude_deadlines():
    pytest.importorskip("pymongo")
    from bson import Int64

    from app.persistence.mongo import _MongoWork
    from app.persistence.mongo_repositories import _MongoScope

    session = object()
    database = {
        name: Mock()
        for name in (
            "users",
            "sessions",
            "collection_entries",
            "loans",
            "reservations",
            "audit_events",
        )
    }
    for collection in database.values():
        collection.find_one.return_value = None
    scope = _MongoScope(database, session, revision=1, writable=False)
    work = _MongoWork(scope)
    database["users"].find_one.return_value = {"_id": "disabled-admin"}
    assert work.accounts.has_admin()
    database["users"].find_one.assert_called_once_with(
        {"role": "admin"}, {"_id": 1}, session=session
    )
    database["users"].find_one.return_value = None

    assert not work.has_activity_after(20)
    for name, fields in (
        ("users", ("created_at",)),
        ("sessions", ("created_at", "last_seen")),
        ("collection_entries", ("created_at",)),
        ("loans", ("requested_at", "approved_at", "returned_at", "cancelled_at")),
        ("reservations", ("created_at", "closed_at")),
        ("audit_events", ("timestamp",)),
    ):
        selector = {"$or": [{field: {"$gt": Int64(20)}} for field in fields]}
        database[name].find_one.assert_called_with(selector, {"_id": 1}, session=session)

    database["audit_events"].find_one.return_value = {"_id": "future-record"}
    assert work.has_activity_after(20)
    scope.close()
    with pytest.raises(RuntimeError, match="closed"):
        work.has_activity_after(20)
