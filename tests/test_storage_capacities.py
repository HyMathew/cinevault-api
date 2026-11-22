"""The JSON v2 cardinality contract at the HTTP boundary of both engines."""

import json
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from mongo_fixtures import related_document

from app import create_app
from app.enterprise_auth import hasher
from app.services.authentication import token_digest
from app.storage_v2 import V2Store, empty_document

TIMESTAMP = 1_700_000_000
PASSWORD = "Synthetic capacity fixture passphrase"
TOKEN = "synthetic-capacity-fixture-token"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(params=("json", "mongodb"))
def backend_client(request, tmp_path):
    if request.param == "json":
        yield lambda document, clock: provision_client(document, clock, tmp_path)
        return

    uri = os.environ.get("MONGODB_TEST_URI")
    if not uri:
        pytest.skip("MONGODB_TEST_URI absent: native capacity contract not executed")
    from pymongo import MongoClient

    with MongoClient(uri, serverSelectionTimeoutMS=5000) as operator:
        database = operator["cinevault_capacity_" + uuid.uuid4().hex]
        try:
            yield lambda document, clock: provision_client(document, clock, tmp_path, database, uri)
        finally:
            database.command("dropAllUsersFromDatabase")
            database.command("dropAllRolesFromDatabase")
            operator.drop_database(database.name)


def provision_client(document, clock, tmp_path, database=None, operator_uri=None):
    config = {
        "TESTING": True,
        "DATA_FILE": str(tmp_path / "legacy.json"),
        "ENTERPRISE_ENABLED": True,
        "AUTH_THROTTLE_KEY": b"x" * 32,
        "AUTH_CLOCK": clock,
    }
    if database is None:
        store = V2Store(tmp_path / "enterprise.json")
        store.path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        config["ENTERPRISE_DATA_FILE"] = str(store.path)
        return create_app(config), store.read

    from app.migrate_backends import read_verified_snapshot
    from app.persistence.migration_codec import encode_snapshot
    from app.persistence.mongo_maintenance import provision

    provision(database)
    snapshot = encode_snapshot(document)
    for name, rows in snapshot.items():
        if name == "coordination":
            database[name].replace_one({"_id": "business"}, rows[0])
        elif rows:
            database[name].insert_many(rows)
    database.command(
        "createRole",
        "application",
        privileges=[
            {
                "resource": {"db": database.name, "collection": name},
                "actions": ["find", "insert", "update", "remove"],
            }
            for name in snapshot
        ],
        roles=[],
    )
    password = secrets.token_urlsafe(24)
    database.command(
        "createUser",
        "application",
        pwd=password,
        mechanisms=["SCRAM-SHA-256"],
        roles=[{"role": "application", "db": database.name}],
    )
    parsed = urlsplit(operator_uri)
    options = dict(parse_qsl(parsed.query))
    options.update(authSource=database.name, authMechanism="SCRAM-SHA-256")
    host = parsed.netloc.rsplit("@", 1)[-1]
    application_uri = urlunsplit(
        (parsed.scheme, f"application:{password}@{host}", "/", urlencode(options), "")
    )
    uri_file = tmp_path / "mongodb.uri"
    uri_file.write_text(application_uri, encoding="utf-8")
    config.update(
        STORAGE_BACKEND="mongodb",
        MONGODB_URI_FILE=str(uri_file),
        MONGODB_DATABASE=database.name,
        REGISTRATION_ADMISSION_FILE=str(tmp_path / "admission.json"),
    )
    try:
        app = create_app(config)
    finally:
        uri_file.unlink()
    return app, lambda: read_verified_snapshot(database)[0]


def fixtures():
    templates = related_document()
    document = empty_document()
    document["revision"] = 1
    owner = {
        **templates["users"][0],
        "password_hash": hasher.hash(PASSWORD),
    }
    movie = templates["movies"][0]
    document["users"] = [owner]
    document["movies"] = [movie, {**movie, "id": str(uuid.UUID(int=20)), "title": "Suite fictive"}]
    document["sessions"] = [
        {
            **templates["sessions"][0],
            "token_hash": token_digest(TOKEN),
            "created_at": TIMESTAMP - 10,
            "last_seen": TIMESTAMP - 10,
            "expires_at": TIMESTAMP + 100,
        }
    ]
    return document, templates


def assert_storage_unavailable(response):
    assert response.status_code == 503
    assert response.json == {
        "error": {"code": "service_unavailable", "message": "Stockage indisponible."}
    }


def test_full_session_storage_rejects_login_until_an_expired_session_is_purged(backend_client):
    document, _ = fixtures()
    session = document["sessions"][0]
    document["sessions"] += [
        {
            **session,
            "id": str(uuid.UUID(int=50 + index)),
            "token_hash": f"{index + 1:064x}",
            "expires_at": TIMESTAMP if index == 0 else session["expires_at"],
        }
        for index in range(9_999)
    ]
    timestamp = TIMESTAMP - 1
    app, read = backend_client(document, lambda: timestamp)
    client = app.test_client()
    before = read()
    payload = {"username": document["users"][0]["username"], "password": PASSWORD}

    assert_storage_unavailable(client.post("/api/v1/auth/login", json=payload))
    assert read() == before

    timestamp += 1
    login = client.post("/api/v1/auth/login", json=payload)
    assert login.status_code == 200
    after = read()
    assert len(after["sessions"]) == 10_000
    hashes = {item["token_hash"] for item in after["sessions"]}
    assert f"{1:064x}" not in hashes
    assert {token_digest(login.json["token"]), token_digest(TOKEN)} <= hashes


def test_private_entry_capacity_is_atomic_and_delete_releases_space(backend_client):
    document, templates = fixtures()
    entry = templates["collections"][0]
    document["collections"] = [
        {**entry, "id": str(uuid.UUID(int=50 + index)), "list_name": f"Liste {index}"}
        for index in range(9_999)
    ]
    app, read = backend_client(document, lambda: TIMESTAMP)
    rendezvous = Barrier(2)

    def create(name):
        rendezvous.wait(timeout=5)
        return app.test_client().post(
            "/api/v1/collections",
            json={"movie_id": entry["movie_id"], "kind": "custom", "list_name": name},
            headers=AUTHORIZATION,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("Première nouvelle", "Seconde nouvelle")))
    accepted, refused = sorted(results, key=lambda response: response.status_code)
    assert accepted.status_code == 201
    assert_storage_unavailable(refused)
    at_capacity = read()
    assert len(at_capacity["collections"]) == 10_000
    assert at_capacity["revision"] == 2
    assert len(at_capacity["audit_events"]) == 1
    accepted_row = accepted.json
    client = app.test_client()
    assert (
        client.delete(
            f"/api/v1/collections/{accepted_row['id']}",
            headers={**AUTHORIZATION, "If-Match": accepted.headers["ETag"]},
        ).status_code
        == 204
    )
    reopened = client.post(
        "/api/v1/collections",
        json={"movie_id": entry["movie_id"], "kind": "custom", "list_name": "Encore une"},
        headers=AUTHORIZATION,
    )
    assert reopened.status_code == 201
    assert len(read()["collections"]) == 10_000


@pytest.mark.parametrize("kind", ("loans", "reservations"))
def test_circulation_history_counts_and_existing_receipt_replays_at_capacity(backend_client, kind):
    document, templates = fixtures()
    if kind == "loans":
        historical = {
            **templates[kind][0],
            "status": "cancelled",
            "copy_id": None,
            "approved_at": None,
            "due_at": None,
            "cancelled_at": 2,
        }
    else:
        historical = {
            **templates[kind][0],
            "status": "cancelled",
            "loan_id": None,
        }
    # Reservation sequence values are global and predate the fixture's initial revision.
    # Use the same valid history in both native stores.
    document["revision"] = 10_000
    document[kind] = [
        {
            **historical,
            "id": str(uuid.UUID(int=50 + index)),
            **({"sequence": index + 1} if kind == "reservations" else {}),
        }
        for index in range(9_999)
    ]
    app, read = backend_client(document, lambda: TIMESTAMP)
    client = app.test_client()
    path = "/api/v1/" + kind
    headers = {**AUTHORIZATION, "Idempotency-Key": "capacity-initial-request"}
    payload = {"movie_id": document["movies"][0]["id"]}
    accepted = client.post(path, json=payload, headers=headers)
    assert accepted.status_code == 201
    before = read()
    assert len(before[kind]) == 10_000

    overflow = client.post(
        path,
        json={"movie_id": document["movies"][1]["id"]},
        headers={**AUTHORIZATION, "Idempotency-Key": "capacity-refused-request"},
    )
    assert_storage_unavailable(overflow)
    assert read() == before

    replay = client.post(path, json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json == accepted.json
    cancelled = client.post(
        f"{path}/{accepted.json['id']}/cancel",
        headers={**AUTHORIZATION, "If-Match": accepted.headers["ETag"]},
    )
    assert cancelled.status_code == 200
    after = read()
    assert len(after[kind]) == 10_000
    assert {item["status"] for item in after[kind]} == {"cancelled"}
