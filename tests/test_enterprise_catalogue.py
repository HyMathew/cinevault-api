from test_enterprise_auth import account
from test_enterprise_auth import enterprise as enterprise

MOVIE = {
    "title": "Dune",
    "year": 2021,
    "director": "Denis Villeneuve",
    "actors": ["Timothée Chalamet"],
    "synopsis": "Un monde de sable.",
    "genres": ["Science-fiction"],
}
COPY = {"support": "blu-ray", "location": "Étagère A", "condition": "good"}


def manager(enterprise):
    client, path = enterprise
    _, auth = account(client, "cataloguemanager", "manager", path)
    return client, path, auth


def test_catalogue_permissions_and_atomic_initial_copy(enterprise):
    client, path, auth = manager(enterprise)
    _, member = account(client, "cataloguemember")
    assert client.get("/api/v1/movies").status_code == 200
    assert client.post("/api/v1/movies", json=MOVIE).status_code == 401
    assert client.post("/api/v1/movies", headers=member, json=MOVIE).status_code == 403
    response = client.post("/api/v1/movies", headers=auth, json={**MOVIE, "initial_copy": COPY})
    assert response.status_code == 201
    movie = response.json
    assert response.headers["Location"].endswith("/" + movie["id"])
    assert response.headers["ETag"] == movie["_etag"]
    copies = client.get("/api/v1/copies", query_string={"movie_id": movie["id"]}).json["copies"]
    assert len(copies) == 1 and copies[0]["support"] == "blu-ray"
    assert (
        client.post("/api/v1/movies", headers=auth, json={**MOVIE, "title": " dune "}).status_code
        == 409
    )
    assert client.get("/api/v1/movies").json["total"] == 1
    assert (
        client.post(
            "/api/v1/movies",
            headers=auth,
            json={**MOVIE, "title": "Bad copy", "initial_copy": {**COPY, "condition": "broken"}},
        ).status_code
        == 422
    )
    assert client.get("/api/v1/movies").json["total"] == 1


def test_optimistic_concurrency_and_explicit_copy_deletion(enterprise):
    client, path, auth = manager(enterprise)
    movie = client.post("/api/v1/movies", headers=auth, json={**MOVIE, "initial_copy": COPY}).json
    endpoint = "/api/v1/movies/" + movie["id"]
    assert client.put(endpoint, headers=auth, json=MOVIE).status_code == 428
    matched = {**auth, "If-Match": movie["_etag"]}
    response = client.put(
        endpoint, headers=matched, json={**MOVIE, "title": "Dune : édition révisée"}
    )
    assert response.status_code == 200
    assert response.json["_etag"] != movie["_etag"]
    assert client.put(endpoint, headers=matched, json=MOVIE).status_code == 412
    assert client.delete(endpoint, headers=matched).status_code == 412
    current = {**auth, "If-Match": response.json["_etag"]}
    assert client.delete(endpoint, headers=current).status_code == 409
    copy = client.get("/api/v1/copies", query_string={"movie_id": movie["id"]}).json["copies"][0]
    copy_url = "/api/v1/copies/" + copy["id"]
    assert client.put(copy_url, headers=auth, json=COPY).status_code == 428
    changed = client.put(
        copy_url, headers={**auth, "If-Match": copy["_etag"]}, json={**COPY, "location": "B"}
    )
    assert changed.status_code == 200
    assert client.delete(copy_url, headers={**auth, "If-Match": copy["_etag"]}).status_code == 412
    assert (
        client.delete(copy_url, headers={**auth, "If-Match": changed.json["_etag"]}).status_code
        == 204
    )
    assert client.delete(endpoint, headers=current).status_code == 204
    assert client.get(endpoint).status_code == 404


def test_filters_pagination_and_public_reads_never_write(enterprise):
    client, path, auth = manager(enterprise)
    for title, year, genre in [
        ("Dune", 2021, "Science-fiction"),
        ("Arrival", 2016, "Science-fiction"),
        ("Zodiac", 2007, "Thriller"),
    ]:
        assert (
            client.post(
                "/api/v1/movies",
                headers=auth,
                json={
                    **MOVIE,
                    "title": title,
                    "year": year,
                    "genres": [genre],
                    "initial_copy": COPY,
                },
            ).status_code
            == 201
        )
    before = path.read_bytes()
    response = client.get(
        "/api/v1/movies",
        query_string={
            "genre": "science-fiction",
            "support": "blu-ray",
            "sort": "year",
            "limit": 1,
            "offset": 1,
        },
    )
    assert response.status_code == 200
    assert response.json["total"] == 2 and response.json["movies"][0]["title"] == "Dune"
    assert (
        client.get("/api/v1/movies", query_string={"q": "chalamet", "year": 2007}).json["total"]
        == 1
    )
    assert path.read_bytes() == before
    for query in (
        {"limit": "101"},
        {"year": "bad"},
        {"sort": "random"},
        {"offset": "-1"},
        {"unknown": "x"},
    ):
        assert client.get("/api/v1/movies", query_string=query).status_code == 422


def test_invalid_references_and_mutations_do_not_corrupt_catalogue(enterprise):
    client, path, auth = manager(enterprise)
    assert (
        client.post(
            "/api/v1/copies", headers=auth, json={**COPY, "movie_id": "unknown"}
        ).status_code
        == 404
    )
    for change in (
        {"year": True},
        {"genres": "wrong"},
        {"genres": ["Drama", "Drama"]},
        {"title": ""},
        {"role": "admin"},
    ):
        assert (
            client.post("/api/v1/movies", headers=auth, json={**MOVIE, **change}).status_code == 422
        )
    assert client.get("/api/v1/movies").json["total"] == 0


def test_catalogue_responses_match_openapi_schemas(enterprise):
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    client, path, auth = manager(enterprise)
    spec = json.loads(
        (Path(__file__).resolve().parents[1] / "docs/openapi-v1.json").read_text(encoding="utf-8")
    )
    movie = client.post("/api/v1/movies", headers=auth, json={**MOVIE, "initial_copy": COPY}).json
    for route, name in [
        ("/api/v1/movies", "MovieList"),
        ("/api/v1/movies/" + movie["id"], "Movie"),
        ("/api/v1/copies", "CopyList"),
    ]:
        schema = {"$ref": "#/components/schemas/" + name, "components": spec["components"]}
        Draft202012Validator(schema).validate(client.get(route).json)
