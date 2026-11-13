"""HTTP circulation uses the shared service and optional actor-key receipts."""

from test_enterprise_auth import enterprise as enterprise
from test_loans import setup_lending


def test_creation_keys_replay_initial_resources_and_share_actor_namespace(enterprise):
    client, _, movie, _, _, owner, _ = setup_lending(enterprise)
    headers = {**owner, "Idempotency-Key": "circulation-http-key-001"}
    data = {"movie_id": movie["id"]}
    first = client.post("/api/v1/loans", headers=headers, json=data)
    replay = client.post("/api/v1/loans", headers=headers, json=data)
    assert first.status_code == replay.status_code == 201
    assert first.json == replay.json
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.headers["Location"] == first.headers["Location"]
    conflict = client.post("/api/v1/reservations", headers=headers, json=data)
    assert conflict.status_code == 409 and conflict.json["error"]["code"] == "operation_conflict"


def test_circulation_http_does_not_use_legacy_document_transaction(enterprise, monkeypatch):
    client, _, movie, _, _, owner, _ = setup_lending(enterprise)

    def forbidden(*args, **kwargs):
        raise AssertionError("Circulation still uses document business mutation")

    monkeypatch.setattr(client.application.extensions["enterprise_store"], "transact", forbidden)
    response = client.post("/api/v1/loans", headers=owner, json={"movie_id": movie["id"]})
    assert response.status_code == 201
    assert client.get(response.headers["Location"], headers=owner).status_code == 200
    assert client.get("/api/v1/reservations", headers=owner).status_code == 200


def test_uncertain_or_unavailable_transactions_are_sanitized_503(enterprise, monkeypatch):
    from app.persistence.mongo_transactions import CommitOutcomeUnknown, TransactionUnavailable

    client, _, movie, _, _, owner, _ = setup_lending(enterprise)
    for problem in (TransactionUnavailable, CommitOutcomeUnknown):

        def unavailable(callback):
            raise problem("sensitive-driver-canary-mongodb://secret")

        monkeypatch.setattr(
            client.application.extensions["enterprise_persistence"], "run", unavailable
        )
        response = client.post("/api/v1/loans", headers=owner, json={"movie_id": movie["id"]})
        assert response.status_code == 503
        assert (
            b"sensitive-driver-canary" not in response.data and b"mongodb://" not in response.data
        )


def test_json_runtime_does_not_require_the_optional_mongodb_driver():
    import subprocess
    import sys

    script = """
import importlib.abc, sys, tempfile
from pathlib import Path
class NoMongo(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('bson', 'pymongo'):
            raise ImportError('Optional driver unavailable')
sys.meta_path.insert(0, NoMongo())
from app import create_app
from app.storage_v2 import V2Store
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    path = folder / 'enterprise.json'
    V2Store(path).initialize()
    app = create_app({'TESTING': True, 'DATA_FILE': str(folder/'legacy.json'),
        'ENTERPRISE_ENABLED': True, 'ENTERPRISE_DATA_FILE': str(path),
        'AUTH_THROTTLE_KEY': b'x'*32})
    assert app.test_client().get('/api/v1/loans').status_code == 401
"""
    outcome = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert outcome.returncode == 0, outcome.stderr


def test_body_size_rejection_does_not_precede_authority_or_loan_precondition(enterprise):
    client, _, movie, _, manager, owner, _ = setup_lending(enterprise)
    loan = client.post("/api/v1/loans", headers=owner, json={"movie_id": movie["id"]}).json
    client.application.config["MAX_CONTENT_LENGTH"] = 3
    oversized = {"data": "xxxxx", "content_type": "application/json"}
    assert client.post("/api/v1/loans", **oversized).status_code == 401
    endpoint = "/api/v1/loans/" + loan["id"] + "/approve"
    assert client.post(endpoint, headers=owner, **oversized).status_code == 403
    assert client.post(endpoint, headers=manager, **oversized).status_code == 428
    assert (
        client.post(
            endpoint, headers={**manager, "If-Match": loan["_etag"]}, **oversized
        ).status_code
        == 413
    )
