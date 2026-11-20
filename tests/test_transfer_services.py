"""Regression contracts for transfer endpoints over shared catalogue repositories."""

from uuid import UUID

import pytest
from test_enterprise_auth import enterprise as enterprise
from test_transfer import KEY, batch, context

from app.storage_v2 import V2Store


def uid(index):
    return str(UUID(int=index))


def catalogue_movie(title, movie_id):
    item = batch(title)["content"]["movies"][0].copy()
    item.pop("copies")
    return {**item, "id": uid(movie_id)}


@pytest.mark.parametrize("policy", ["skip", "append_copies"])
def test_ambiguous_legacy_key_is_a_conflict_without_copy_or_receipt(enterprise, policy):
    client, path, manager, _ = context(enterprise)
    store = V2Store(path)
    store.transact(
        lambda doc: doc["movies"].extend(
            [catalogue_movie("Imported film", 2), catalogue_movie("Imported film", 1)]
        )
    )
    before = store.read()
    payload = {**batch(), "duplicate_policy": policy}
    response = client.post(
        "/api/v1/catalogue/import", headers={**manager, "Idempotency-Key": KEY}, json=payload
    )
    assert response.status_code == 409
    assert response.json["error"]["code"] == "import_conflict"
    after = store.read()
    assert after["movies"] == before["movies"]
    assert after["copies"] == after["idempotency_records"] == []
    assert [event["action"] for event in after["audit_events"]] == ["import.failed"]


def test_catalogue_transfer_requires_only_the_shared_persistence(enterprise):
    client, _, manager, _ = context(enterprise)
    client.application.extensions.pop("enterprise_store")
    response = client.post(
        "/api/v1/catalogue/import", headers={**manager, "Idempotency-Key": KEY}, json=batch()
    )
    assert response.status_code == 200
    exported = client.get("/api/v1/catalogue/export", headers=manager)
    assert exported.status_code == 200
    assert [m["title"] for m in exported.json["content"]["movies"]] == ["Imported film"]


def test_export_orders_movies_and_copies_by_uuid(enterprise):
    client, path, manager, _ = context(enterprise)
    store = V2Store(path)
    attrs = batch()["content"]["movies"][0]["copies"][0]

    def seed(doc):
        doc["movies"].extend(
            [catalogue_movie("First inserted", 20), catalogue_movie("Second inserted", 10)]
        )
        doc["copies"].extend([{**attrs, "id": uid(i), "movie_id": uid(10)} for i in (35, 30)])

    store.transact(seed)
    movies = client.get("/api/v1/catalogue/export", headers=manager).json["content"]["movies"]
    assert [row["id"] for row in movies] == [uid(10), uid(20)]
    assert [row["id"] for row in movies[0]["copies"]] == [uid(30), uid(35)]


def test_preexisting_import_receipt_replays_historical_carrier_digest(enterprise):
    import json

    from app.receipt_storage import digest
    from app.services.receipts import key_digest

    client, path, manager, _ = context(enterprise)
    store = V2Store(path)
    content = " " + json.dumps(batch()["content"], indent=1)
    body = {"format": "json", "content": content}
    historical = {
        "operation_id": uid(81),
        "created_movies": 1,
        "created_copies": 1,
        "skipped_movies": 0,
        "skipped_copies": 0,
    }

    def seed(doc):
        actor = next(row["id"] for row in doc["users"] if row["role"] == "manager")
        session = next(row for row in doc["sessions"] if row["user_id"] == actor)
        timestamp = session["created_at"]
        doc["idempotency_records"].append(
            {
                "id": uid(80),
                "actor_id": actor,
                "operation": "catalog.import",
                "key_hash": key_digest(KEY),
                "request_hash": digest({**body, "duplicate_policy": "reject"}),
                "created_at": timestamp,
                "expires_at": timestamp + 86400,
                "response": historical,
            }
        )

    store.transact(seed)
    response = client.post(
        "/api/v1/catalogue/import", headers={**manager, "Idempotency-Key": KEY}, json=body
    )
    assert response.status_code == 200
    assert response.headers["Idempotency-Replayed"] == "true"
    assert response.json == historical
    state = store.read()
    assert state["movies"] == state["copies"] == state["audit_events"] == []


def test_prepare_once_and_retry_reuses_every_written_identity(enterprise):
    from app.persistence.json import JsonPersistence
    from app.services.catalogue_transfer import CatalogueTransferService
    from app.services.receipts import key_digest

    _, path, manager, _ = context(enterprise)
    backend = JsonPersistence(path)
    store = V2Store(path)
    initial = store.read()
    actor = next(row["id"] for row in initial["users"] if row["role"] == "manager")
    timestamp = next(row["last_seen"] for row in initial["sessions"] if row["user_id"] == actor)
    attempts, external_reads = [], []

    class Abort(Exception):
        pass

    class Retry:
        query = backend.query

        def run(self, command):
            def record(work):
                assert external_reads == [1]
                result = command(work)
                attempts.append(
                    (
                        result,
                        work.catalogue.export_records(),
                        work.receipts.get(actor, key_digest(KEY)),
                        work.events.page(1, 0),
                    )
                )
                return result

            def abort(work):
                record(work)
                raise Abort()

            with pytest.raises(Abort):
                backend.run(abort)
            assert store.read() == initial
            return backend.run(record)

    def read_body():
        external_reads.append(1)
        return batch()

    result = CatalogueTransferService(Retry()).import_catalogue(
        manager["Authorization"].split()[1], read_body, key=KEY, timestamp=timestamp
    )
    assert result.status == 200
    assert attempts[0] == attempts[1]
    assert len(store.read()["idempotency_records"]) == 1


def test_rechecks_authority_after_preparing_import(enterprise):
    from app.persistence.json import JsonPersistence
    from app.services.catalogue_transfer import CatalogueTransferService

    _, path, manager, _ = context(enterprise)
    backend = JsonPersistence(path)
    store = V2Store(path)
    initial = store.read()
    actor = next(row["id"] for row in initial["users"] if row["role"] == "manager")
    timestamp = next(row["last_seen"] for row in initial["sessions"] if row["user_id"] == actor)

    def revoke_before_returning_body():
        backend.run(lambda work: work.sessions.revoke_user(actor))
        return batch()

    result = CatalogueTransferService(backend).import_catalogue(
        manager["Authorization"].split()[1],
        revoke_before_returning_body,
        key=KEY,
        timestamp=timestamp,
    )
    assert result.status == 401
    state = store.read()
    assert state["movies"] == state["copies"] == state["audit_events"] == []
    assert state["idempotency_records"] == []


def test_rejects_role_before_transport_read_and_audits_only_semantic_failure(enterprise):
    from app.persistence.json import JsonPersistence
    from app.services.catalogue_transfer import CatalogueTransferService

    client, path, manager, member = context(enterprise)
    backend = JsonPersistence(path)
    store = V2Store(path)
    timestamp = max(row["last_seen"] for row in store.read()["sessions"])
    before = store.read()

    def forbidden_read():
        pytest.fail("An unprivileged account must not cause the file to be read")

    for auth, status in (({}, 401), (member, 403)):
        token = auth.get("Authorization", "Bearer ").removeprefix("Bearer ")
        assert (
            CatalogueTransferService(backend)
            .import_catalogue(token, forbidden_read, key=KEY, timestamp=timestamp)
            .status
            == status
        )
    assert store.read()["revision"] == before["revision"] + 1
    route = "/api/v1/catalogue/import"
    assert (
        client.post(route, headers=manager, data="{", content_type="application/json").status_code
        == 400
    )
    assert client.post(route, headers=manager, json={}).status_code == 422
    assert client.post(route, headers=manager, json=batch()).status_code == 428
    assert store.read()["audit_events"] == []
    assert (
        client.post(route, headers=manager, json={"format": "json", "content": "{"}).status_code
        == 422
    )
    assert [event["action"] for event in store.read()["audit_events"]] == ["import.failed"]
