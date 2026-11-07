from unittest.mock import Mock

import pytest
from test_enterprise_auth import account
from test_enterprise_auth import enterprise as enterprise
from test_enterprise_catalogue import MOVIE

FILM = {
    "id": "Q151599",
    "revision": 123,
    "retrieved_at": 1700000000,
    "title": "Metropolis",
    "year": 1927,
    "director": "Fritz Lang",
    "actors": [],
    "genres": ["Science-fiction"],
    "description": "Film de 1927",
    "warnings": [],
    "poster": None,
    "poster_filename": None,
    "source_url": "https://www.wikidata.org/wiki/Q151599",
}


def context(enterprise):
    client, path = enterprise
    _, manager = account(client, "metadata-manager", "manager", path)
    _, member = account(client, "metadata-member")
    provider = Mock()
    provider.search.return_value = {
        "items": [],
        "cache": {"status": "fresh", "hit": False, "stored_at": 1700000000},
    }
    provider.detail.return_value = {
        "film": FILM.copy(),
        "cache": {"status": "fresh", "hit": False, "stored_at": 1700000000},
    }
    client.application.extensions["metadata_client"] = provider
    return client, path, manager, member, provider


def test_metadata_roles_disabled_feature_and_network_outside_store_lock(enterprise, monkeypatch):
    client, path, manager, member, provider = context(enterprise)
    route = "/api/v1/metadata/search?q=Metropolis"
    assert client.get(route).status_code == 401
    assert client.get(route, headers=member).status_code == 403
    provider.search.assert_not_called()
    store = client.application.extensions["enterprise_store"]
    inside = [False]
    original = store.transact

    def transaction(operation):
        inside[0] = True
        try:
            return original(operation)
        finally:
            inside[0] = False

    monkeypatch.setattr(store, "transact", transaction)

    def external(query):
        assert inside[0] is False
        return {"items": [], "cache": {"status": "fresh", "hit": False, "stored_at": 1700000000}}

    provider.search.side_effect = external
    assert client.get(route, headers=manager).status_code == 200
    client.application.extensions["metadata_client"] = None
    assert client.get(route, headers=manager).status_code == 503
    assert client.get("/api/v1/movies").status_code == 200


def test_enrichment_source_is_server_verified_and_roundtrips_json_and_csv(enterprise):
    client, path, manager, member, provider = context(enterprise)
    created = client.post(
        "/api/v1/movies", headers=manager, json={**MOVIE, "external_id": "Q151599"}
    )
    assert created.status_code == 201
    source = {"qid": "Q151599", "revision": 123, "retrieved_at": 1700000000}
    assert created.json["external_source"] == source
    provider.detail.assert_called_once_with("Q151599")
    assert (
        client.post(
            "/api/v1/movies",
            headers=manager,
            json={**MOVIE, "title": "forged", "external_source": source},
        ).status_code
        == 422
    )
    endpoint = "/api/v1/movies/" + created.json["id"]
    updated = client.put(
        endpoint,
        headers={**manager, "If-Match": created.json["_etag"]},
        json={**MOVIE, "synopsis": "Human edited synopsis"},
    )
    assert updated.status_code == 200 and updated.json["external_source"] == source
    exported = client.get("/api/v1/catalogue/export", headers=manager).json["content"]
    assert exported["movies"][0]["external_source"] == source
    from app.catalogue_transfer import normalize, parse_csv

    csv = client.get("/api/v1/catalogue/export?format=csv", headers=manager).json["content"]
    assert normalize(parse_csv(csv))[0]["external_source"] == source
    removed = client.put(
        endpoint,
        headers={**manager, "If-Match": updated.json["_etag"]},
        json={**MOVIE, "external_id": None},
    )
    assert removed.status_code == 200 and removed.json["external_source"] is None


def test_source_validation_failure_and_permissions_rechecked_after_fetch(enterprise):
    client, path, manager, member, provider = context(enterprise)
    from app.metadata import MetadataBusy, MetadataUnavailable

    provider.detail.side_effect = MetadataBusy(120)
    response = client.get("/api/v1/metadata/Q151599", headers=manager)
    assert response.status_code == 429 and response.headers["Retry-After"] == "120"
    provider.detail.side_effect = MetadataUnavailable()
    assert (
        client.post(
            "/api/v1/movies", headers=manager, json={**MOVIE, "external_id": "Q151599"}
        ).status_code
        == 503
    )
    assert client.get("/api/v1/movies").json["total"] == 0
    from app.storage_v2 import V2Store

    def demote(identity):
        V2Store(path).transact(
            lambda doc: next(u for u in doc["users"] if u["username"] == "metadata-manager").update(
                role="member"
            )
        )
        return {
            "film": FILM.copy(),
            "cache": {"status": "fresh", "hit": False, "stored_at": 1700000000},
        }

    provider.detail.side_effect = demote
    assert (
        client.post(
            "/api/v1/movies", headers=manager, json={**MOVIE, "external_id": "Q151599"}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/movies").json["total"] == 0


def test_public_posters_only_use_fresh_local_cache_for_selected_revision(enterprise):
    client, path, manager, member, provider = context(enterprise)
    created = client.post(
        "/api/v1/movies", headers=manager, json={**MOVIE, "external_id": "Q151599"}
    ).json
    poster = {
        "image_url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Test.jpg/500px-Test.jpg",
        "source_url": "https://commons.wikimedia.org/wiki/File:Test.jpg",
        "artist": "Artist",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution_required": True,
    }
    provider.cache.peek_many.return_value = {"detail:Q151599": {"film": {**FILM, "poster": poster}}}
    response = client.get("/api/v1/posters", query_string={"movie_id": created["id"]})
    assert response.status_code == 200 and response.json["posters"][created["id"]] == poster
    provider.search.assert_not_called()
    assert provider.detail.call_count == 1
    provider.cache.peek_many.return_value = {
        "detail:Q151599": {"film": {**FILM, "revision": 999, "poster": poster}}
    }
    assert (
        client.get("/api/v1/posters", query_string={"movie_id": created["id"]}).json["posters"]
        == {}
    )
    provider.cache.peek_many.return_value = {}
    assert (
        client.get("/api/v1/posters", query_string={"movie_id": created["id"]}).json["posters"]
        == {}
    )
    provider.cache.peek_many.return_value = {
        "detail:Q151599": {
            "film": {**FILM, "poster": {**poster, "image_url": "javascript:alert(1)"}}
        }
    }
    assert (
        client.get("/api/v1/posters", query_string={"movie_id": created["id"]}).json["posters"]
        == {}
    )


def test_source_shape_and_metadata_configuration_are_strict(enterprise, tmp_path):
    import copy
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from app import create_app
    from app.storage_v2 import V2Store, validate_document

    client, path, manager, member, provider = context(enterprise)
    movie = client.post(
        "/api/v1/movies", headers=manager, json={**MOVIE, "external_id": "Q151599"}
    ).json
    document = V2Store(path).read()
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/catalogue-v2.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(document)
    api = json.loads(
        (Path(__file__).resolve().parents[1] / "docs/openapi-v1.json").read_text(encoding="utf-8")
    )
    for route, name, headers in (
        ("/api/v1/metadata/search?q=Metropolis", "MetadataSearch", manager),
        ("/api/v1/metadata/Q151599", "MetadataDetail", manager),
        ("/api/v1/movies/" + movie["id"], "Movie", {}),
        ("/api/v1/posters?movie_id=" + movie["id"], "Posters", {}),
    ):
        if name == "Posters":
            provider.cache.peek_many.return_value = {}
        result = client.get(route, headers=headers)
        assert result.status_code == 200
        Draft202012Validator(
            {"$ref": "#/components/schemas/" + name, "components": api["components"]}
        ).validate(result.json)
    for source in (
        {"qid": "http://localhost", "revision": 1, "retrieved_at": 0},
        {"qid": "Q151599", "revision": True, "retrieved_at": 0},
        {"qid": "Q151599", "revision": 1, "retrieved_at": 0, "token": "forbidden"},
    ):
        bad = copy.deepcopy(document)
        bad["movies"][0]["external_source"] = source
        with pytest.raises(ValueError):
            validate_document(bad)
    config = {
        "TESTING": True,
        "DATA_FILE": str(tmp_path / "legacy.json"),
        "ENTERPRISE_ENABLED": True,
        "ENTERPRISE_DATA_FILE": str(path),
        "AUTH_THROTTLE_KEY": b"x" * 32,
        "METADATA_ENABLED": True,
        "WIKIMEDIA_USER_AGENT": "CineVault/0.1 (https://example.test/contact)",
    }
    for target in (str(path), str(path) + ".lock", config["DATA_FILE"]):
        with pytest.raises(RuntimeError):
            create_app({**config, "METADATA_CACHE_FILE": target})
    with pytest.raises(RuntimeError):
        create_app({**config, "WIKIMEDIA_USER_AGENT": "invalid"})
    app = create_app({**config, "METADATA_CACHE_FILE": str(tmp_path / "cache.json")})
    assert app.extensions["metadata_client"] is not None
    assert not (tmp_path / "cache.json").exists()  # No network/cache mutation at startup.
