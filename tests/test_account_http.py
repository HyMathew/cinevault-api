"""Identity HTTP boundaries use the common services and preserve rejection order."""

import pytest
from test_enterprise_auth import enterprise as enterprise

from app.bootstrap import bootstrap_admin
from app.storage_v2 import V2Store

PASSWORD = "long passphrase 123"


def test_all_identity_routes_work_without_the_document_route_store(enterprise):
    client, path = enterprise
    admin = bootstrap_admin(V2Store(path), "admin", "Admin", PASSWORD)
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
    headers = {"Authorization": "Bearer " + login.json["token"]}

    class ForbiddenDocumentStore:
        def read(self):
            pytest.fail("Identity route read the transitional document store")

        def transact(self, command):
            pytest.fail("Identity route wrote the transitional document store")

    client.application.extensions["enterprise_store"] = ForbiddenDocumentStore()
    created = client.post(
        "/api/v1/auth/register",
        json={
            "username": "member",
            "display_name": "Member",
            "password": PASSWORD,
        },
    )
    assert created.status_code == 201
    member_id = created.json["id"]
    listing = client.get("/api/v1/users?limit=1&limit=bad", headers=headers)
    assert listing.status_code == 200 and listing.json["total"] == 2
    assert client.get("/api/v1/users/" + member_id, headers=headers).json == created.json
    changed = client.patch("/api/v1/users/" + member_id, headers=headers, json={"role": "manager"})
    assert changed.status_code == 200 and changed.json["role"] == "manager"
    last = client.patch("/api/v1/users/" + admin["id"], headers=headers, json={"enabled": False})
    assert (last.status_code, last.json["error"]["code"]) == (409, "last_admin")


def test_admin_http_rejections_preserve_authority_payload_and_session_rollback(enterprise):
    client, path = enterprise
    bootstrap_admin(V2Store(path), "admin", "Admin", PASSWORD)
    client.post(
        "/api/v1/auth/register",
        json={
            "username": "member",
            "display_name": "Member",
            "password": PASSWORD,
        },
    )
    tokens = {
        name: client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD}).json[
            "token"
        ]
        for name in ("member", "admin")
    }
    before = path.read_bytes()
    for token, expected in (("invalid", 401), (tokens["member"], 403), (tokens["admin"], 415)):
        response = client.patch(
            "/api/v1/users/not-a-uuid",
            headers={"Authorization": "Bearer " + token},
            data="not-json",
        )
        assert response.status_code == expected
        if expected in (401, 415):
            assert path.read_bytes() == before
        before = path.read_bytes()
    response = client.patch(
        "/api/v1/users/not-a-uuid",
        headers={"Authorization": "Bearer " + tokens["admin"]},
        json={"enabled": True},
    )
    assert response.status_code == 404
