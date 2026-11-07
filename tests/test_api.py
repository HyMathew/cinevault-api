import pytest

from app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app({"TESTING": True, "DATA_FILE": str(tmp_path / "films.json")})
    return app.test_client()


@pytest.fixture
def film():
    return {
        "titre": " Étoile ",
        "annee": 2001,
        "realisateur": "A. Martin",
        "acteurs": ["B. Durand"],
        "support": "dvd",
        "synopsis": "Cinéma",
    }


def test_health_reports_healthy_storage(tmp_path):
    app = create_app({"TESTING": True, "DATA_FILE": str(tmp_path / "films.json")})
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_crud_search_filter_and_persistence(tmp_path, film):
    config = {"TESTING": True, "DATA_FILE": str(tmp_path / "films.json")}
    client = create_app(config).test_client()
    created = client.post("/api/films", json=film)
    assert created.status_code == 201
    item = created.json
    assert item["titre"] == "Étoile"
    assert created.headers["Location"].endswith(item["id"])
    path = "/api/films/" + item["id"]
    assert create_app(config).test_client().get(path).json == item
    assert len(client.get("/api/films?q=ÉTOILE&support=dvd").json["films"]) == 1
    assert client.get("/api/films?q=absent").json["films"] == []
    film["titre"] = "Autre"
    assert client.put(path, json=film).json["titre"] == "Autre"
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        {"titre": ""},
        {"annee": True},
        {"annee": 1887},
        {"realisateur": ""},
        {"acteurs": [""]},
        {"support": "dvdx"},
        {"unknown": 1},
        {"synopsis": "x" * 4001},
    ],
)
def test_invalid_input_is_rejected(client, film, change):
    film.update(change)
    response = client.post("/api/films", json=film)
    assert response.status_code == 422
    assert response.json["error"]["code"] == "validation_error"


def test_request_errors_are_json(client, film):
    assert (
        client.post("/api/films", data="oops", content_type="application/json").status_code == 400
    )
    assert client.post("/api/films", data="{}", content_type="text/plain").status_code == 415
    assert (
        client.post(
            "/api/films", data='{"titre":"x","titre":"y"}', content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        client.post("/api/films", data="x" * 65537, content_type="application/json").status_code
        == 413
    )
    assert client.patch("/api/films").status_code == 405
    assert client.get("/api/films/nope").status_code == 404
    assert client.put("/api/films/nope", json=film).status_code == 404
    assert client.delete("/api/films/nope").status_code == 404
    assert client.patch("/api/films").is_json


def test_corrupt_file_is_preserved_and_unhealthy(tmp_path):
    path = tmp_path / "films.json"
    path.write_text("{broken", encoding="utf-8")
    client = create_app({"TESTING": True, "DATA_FILE": str(path)}).test_client()
    assert client.get("/health").status_code == 503
    assert client.get("/api/films").status_code == 503
    assert path.read_text(encoding="utf-8") == "{broken"


def test_method_not_allowed_retains_allow_header(client):
    response = client.patch("/api/films")
    assert response.status_code == 405
    assert {"GET", "POST"} <= set(response.headers["Allow"].replace(" ", "").split(","))


@pytest.mark.parametrize("field", ["titre", "realisateur", "acteurs", "synopsis"])
def test_surrogate_is_rejected_without_changing_store(tmp_path, film, field):
    path = tmp_path / "films.json"
    client = create_app({"TESTING": True, "DATA_FILE": str(path)}).test_client()
    assert client.get("/health").status_code == 200
    before = path.read_bytes()
    film[field] = ["\ud800"] if field == "acteurs" else "\ud800"
    response = client.post("/api/films", json=film)
    assert response.status_code == 422
    assert response.json["error"]["code"] == "validation_error"
    assert path.read_bytes() == before


def test_deeply_nested_json_returns_client_error(client):
    response = client.post(
        "/api/films", data="[" * 20000 + "]" * 20000, content_type="application/json"
    )
    assert response.status_code == 400
    assert response.is_json
