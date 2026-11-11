"""One public catalogue contract on a real JSON file or a supplied native database."""

import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path

from app.persistence.json import JsonPersistence
from app.storage_v2 import V2Store
from app.validation import ValidationError


def service_type():
    assert importlib.util.find_spec("app.services.public_catalogue") is not None, (
        "Public catalogue queries must use a pure shared service"
    )
    from app.services.public_catalogue import PublicCatalogueService

    return PublicCatalogueService


def fixtures(backend):
    marker = uuid.uuid4().hex[:12]
    drama = "DRAME_" + marker
    movies = [
        {
            "id": str(uuid.uuid4()),
            "title": title,
            "year": year,
            "director": director,
            "actors": actors,
            "genres": genres,
            "synopsis": "Résumé",
        }
        for title, year, director, actors, genres in (
            (
                marker + " Straße",
                2021,
                "Auteur Σ " + marker,
                ["Fin", "Début"],
                [drama, "Σς_" + marker],
            ),
            (marker + " STRASSE", 2022, "Autre", [], ["Comédie"]),
            (marker + " .*[x]", 2023, "Test", ["A\rLF\nNUL\0🎬" + marker], [drama]),
            (marker + " ΐ" * 10, 2024, "Unicode", ["Nom, composé"], []),
        )
    ]
    copies = [
        {
            "id": str(uuid.uuid4()),
            "movie_id": movie["id"],
            "support": support,
            "location": "A",
            "condition": "good",
        }
        for movie, support in ((movies[0], "dvd"), (movies[0], "vhs"), (movies[1], "dvd"))
    ]
    backend.run(
        lambda work: (
            [work.catalogue.insert_movie(movie) for movie in movies],
            [work.catalogue.insert_copy(item) for item in copies],
        )
    )
    return movies, copies


def exercise_public_queries(backend, *, observer=None):
    service = service_type()(backend)
    movies, copies = fixtures(backend)
    before = backend.query(lambda work: work.revision)
    if observer is not None:
        observer.clear()
    listing = service.movies([("q", movies[0]["title"].split(" ", 1)[0])])
    assert listing.status == 200 and listing.body["total"] == 4
    ids = [item["id"] for item in listing.body["movies"]]
    expected = sorted(movies, key=lambda row: (row["title"].casefold(), row["id"]))
    assert [identity for identity in ids if identity in {m["id"] for m in movies}] == [
        m["id"] for m in expected
    ]
    for query, expected_ids in (
        ([("q", movies[0]["title"].upper())], {movies[0]["id"], movies[1]["id"]}),
        ([("q", movies[0]["director"] + " fin début")], {movies[0]["id"]}),
        ([("q", movies[2]["title"].removesuffix("[x]"))], {movies[2]["id"]}),
        ([("q", movies[2]["actors"][0].removeprefix("A\r"))], {movies[2]["id"]}),
        ([("genre", movies[0]["genres"][1].casefold())], {movies[0]["id"]}),
        ([("support", "vhs"), ("q", movies[0]["title"])], {movies[0]["id"]}),
        (
            [("genre", movies[0]["genres"][0].lower()), ("support", "dvd"), ("year", "2021")],
            {movies[0]["id"]},
        ),
    ):
        result = service.movies(query)
        assert {m["id"] for m in result.body["movies"]} == expected_ids
        assert result.body["total"] == len(expected_ids)
    page = service.movies(
        [("q", movies[0]["title"]), ("sort", "-year"), ("offset", "1"), ("limit", "1")]
    )
    assert page.body["total"] == 2 and page.body["movies"][0]["id"] == movies[0]["id"]
    reverse = service.movies([("q", movies[0]["title"]), ("sort", "-title")])
    expected_reverse = sorted(
        movies[:2], key=lambda row: (row["title"].casefold(), row["id"]), reverse=True
    )
    assert [m["id"] for m in reverse.body["movies"]] == [m["id"] for m in expected_reverse]
    summary = listing.body["copy_summaries"][movies[0]["id"]]
    assert summary["count"] == 2 and set(summary["supports"]) == {"dvd", "vhs"}
    assert (
        service.movie(movies[0]["id"]).headers["ETag"]
        == service.movie(movies[0]["id"]).body["_etag"]
    )
    assert service.copy(copies[0]["id"]).body["movie_id"] == movies[0]["id"]
    assert len(service.copies([("movie_id", movies[0]["id"])]).body["copies"]) == 2
    assert service.copies([("movie_id", "not-uuid")]).body == {"copies": []}
    assert service.movie("not-uuid").status == service.copy("not-uuid").status == 404
    assert backend.query(lambda work: work.revision) == before
    assert all(
        not any(key.endswith("_key") or key == "genre_keys" for key in m)
        for m in listing.body["movies"]
    )
    traced = verify_read_trace(observer)
    extra = [
        {
            "id": str(uuid.uuid4()),
            "movie_id": movies[0]["id"],
            "support": "dvd",
            "condition": "good",
            "location": str(number),
        }
        for number in range(105)
    ]
    backend.run(lambda work: [work.catalogue.insert_copy(item) for item in extra])
    before = backend.query(lambda work: work.revision)
    if observer is not None:
        observer.clear()
    all_copies = service.copies([]).body["copies"]
    assert {item["id"] for item in [*copies, *extra]}.issubset({item["id"] for item in all_copies})
    assert len(service.copies([("movie_id", movies[0]["id"])]).body["copies"]) == 107
    assert backend.query(lambda work: work.revision) == before
    return {
        "public_catalogue": "passed",
        "_fixture_movie_id": movies[0]["id"],
        "native_public_read_trace": {
            "checked": observer is not None,
            "commands": traced + verify_read_trace(observer),
        },
        "literal_unicode_cross_field_search": True,
        "server_pagination_contract": True,
        "read_only_revision": True,
        "copies_not_truncated_after_100": True,
    }


def verify_read_trace(observer):
    if observer is None:
        return 0
    assert observer, "Native public read command trace is empty"
    for command, collection in observer:
        assert command in {"find", "aggregate"}, "Public read issued a mutation"
        assert collection in {"coordination", "movies", "copies"}, (
            "Public read accessed another collection"
        )
    return len(observer)


class PublicCatalogueServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.json"
        V2Store(self.path).initialize()
        self.backend = JsonPersistence(self.path)

    def test_shared_public_contract_on_real_json(self):
        self.assertEqual(exercise_public_queries(self.backend)["public_catalogue"], "passed")

    def test_native_trace_rejects_mutations_and_private_collection_reads(self):
        for events in (
            [("find", "users")],
            [("insert", "movies")],
            [("aggregate-write", "copies")],
            [],
        ):
            with self.assertRaises(AssertionError):
                verify_read_trace(events)
        self.assertEqual(
            verify_read_trace(
                [("find", "coordination"), ("aggregate", "movies"), ("aggregate", "copies")]
            ),
            3,
        )

    def test_invalid_parameters_are_rejected_without_reading_storage(self):
        class NoRead:
            def query(self, callback):
                raise AssertionError("Invalid request reached persistence")

        service = service_type()(NoRead())
        for parameters in (
            [("sort", "random")],
            [("q", "x" * 201)],
            [("genre", "x" * 81)],
            [("limit", "101")],
            [("offset", "-1")],
            [("year", "1" * 5000)],
            [("support", "anything")],
            [("limit", "1"), ("limit", "2")],
            [("unknown", "x")],
        ):
            with self.subTest(parameters=parameters[0][0]), self.assertRaises(ValidationError):
                service.movies(parameters)
        for parameters in ([("unknown", "x")], [("movie_id", "x"), ("movie_id", "y")]):
            with self.assertRaises(ValidationError):
                service.copies(parameters)

    def test_all_copies_are_returned_without_an_implicit_100_item_limit(self):
        service = service_type()(self.backend)
        movies, _ = fixtures(self.backend)
        self.backend.run(
            lambda work: [
                work.catalogue.insert_copy(
                    {
                        "id": str(uuid.uuid4()),
                        "movie_id": movies[0]["id"],
                        "support": "dvd",
                        "condition": "good",
                        "location": str(number),
                    }
                )
                for number in range(105)
            ]
        )
        before = self.path.read_bytes()
        self.assertEqual(len(service.copies([]).body["copies"]), 108)
        self.assertEqual(len(service.copies([("movie_id", movies[0]["id"])]).body["copies"]), 107)
        self.assertEqual(self.path.read_bytes(), before)


def exercise_public_catalogue_service(database, *, observer=None):
    from bson import Int64
    from pymongo.errors import OperationFailure

    from app.persistence.mongo import MongoPersistence

    result = exercise_public_queries(MongoPersistence(database), observer=observer)
    raw = database.movies.find_one({"_id": result.pop("_fixture_movie_id")})
    invalid = {**raw, "_id": str(uuid.uuid4())}
    invalid.pop("search_key")
    try:
        database.movies.insert_one(invalid)
    except OperationFailure as exc:
        assert exc.code == 121
    else:
        raise AssertionError("Native validation accepted a missing movie search key")
    finally:
        database.movies.delete_one({"_id": invalid["_id"]})
    database.movies.update_one({"_id": raw["_id"]}, {"$set": {"title_key": "tampered"}})
    try:
        try:
            service_type()(MongoPersistence(database)).movie(raw["_id"])
        except ValueError:
            pass
        else:
            raise AssertionError("A tampered movie key was silently accepted")
    finally:
        database.movies.update_one({"_id": raw["_id"]}, {"$set": {"title_key": raw["title_key"]}})
    result["native_missing_key_refused"] = True
    result["native_tampered_key_read_refused"] = True
    plans = {}
    for name, request in (
        (
            "literal_substring",
            {"find": "movies", "filter": {"search_key": {"$regex": r"\.\*\[x\]"}}},
        ),
        (
            "year_order",
            {
                "find": "movies",
                "filter": {"year": Int64(2021)},
                "sort": {"year": 1, "_id": 1},
                "limit": 1,
            },
        ),
        (
            "natural_duplicate",
            {
                "find": "movies",
                "filter": {
                    "year": Int64(2021),
                    "title_key": raw["title_key"],
                    "director_key": raw["director_key"],
                },
            },
        ),
    ):
        explanation = database.command("explain", request, verbosity="queryPlanner")
        stages, indexes = set(), set()

        def visit(node):
            if isinstance(node, dict):
                if "stage" in node:
                    stages.add(node["stage"])
                if "indexName" in node:
                    indexes.add(node["indexName"])
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(explanation["queryPlanner"]["winningPlan"])
        plans[name] = {"stages": sorted(stages), "indexes": sorted(indexes)}
    result["tiny_fixture_query_plans"] = plans
    return result


def test_public_http_uses_query_ports_without_legacy_read_or_mutation(tmp_path, monkeypatch):
    from app import create_app
    from app.services.results import ServiceResult

    path = tmp_path / "store.json"
    V2Store(path).initialize()
    backend = JsonPersistence(path)
    movies, copies = fixtures(backend)
    app = create_app(
        {
            "TESTING": True,
            "DATA_FILE": str(tmp_path / "legacy.json"),
            "ENTERPRISE_ENABLED": True,
            "ENTERPRISE_DATA_FILE": str(path),
            "AUTH_THROTTLE_KEY": b"x" * 32,
        }
    )
    original_query = app.extensions["enterprise_persistence"].query
    observed = []

    def query(callback):
        def pure(work):
            result = callback(work)
            assert isinstance(result, ServiceResult)
            observed.append(result.status)
            return result

        return original_query(pure)

    def forbidden(*args, **kwargs):
        raise AssertionError("Public query used legacy read or mutation")

    monkeypatch.setattr(app.extensions["enterprise_persistence"], "query", query)
    monkeypatch.setattr(app.extensions["enterprise_persistence"], "run", forbidden)
    monkeypatch.setattr(app.extensions["enterprise_store"], "read", forbidden)
    monkeypatch.setattr(app.extensions["enterprise_store"], "transact", forbidden)
    client = app.test_client()
    before = path.read_bytes()
    assert client.get("/api/v1/movies", query_string={"q": "strasse"}).json["total"] == 2
    for endpoint in ("/api/v1/movies/" + movies[0]["id"], "/api/v1/copies/" + copies[0]["id"]):
        response = client.get(endpoint)
        assert response.status_code == 200 and response.headers["ETag"] == response.json["_etag"]
    assert len(client.get("/api/v1/copies").json["copies"]) == 3
    assert observed == [200, 200, 200, 200]
    assert path.read_bytes() == before
