"""Transport-free structural checks for the targeted MongoDB transfer ports."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from test_enterprise_catalogue import COPY, MOVIE


def uid(index):
    return str(UUID(int=index))


class Collection:
    """An in-memory driver boundary, recording exact selectors and session identity."""

    def __init__(self, session):
        self.session = session
        self.rows = {}
        self.inserts = []
        self.reads = []
        self.history_conflict = None

    def insert_many(self, rows, *, ordered, session):
        assert session is self.session and ordered and len(rows) <= 500
        self.inserts.append(len(rows))
        for row in rows:
            assert row["_id"] not in self.rows
            self.rows[row["_id"]] = row

    def find(self, selector, projection, *, session):
        assert session is self.session
        requested = selector["_id"]["$in"]
        assert len(requested) <= 500
        self.reads.append(len(requested))
        rows = [self.rows[identity] for identity in requested if identity in self.rows]
        if projection == {"_id": 1}:
            rows = [{"_id": row["_id"]} for row in rows]
        return nullcontext(rows)

    def find_one(self, selector, projection, *, session):
        assert session is self.session and projection == {"_id": 1}
        conditions = selector["$or"]
        assert len(conditions) <= 500 and {k for condition in conditions for k in condition} == {
            "copy_id",
            "movie_id",
        }
        self.reads.append(len(conditions))
        return self.history_conflict


def boundary():
    from app.persistence.mongo_repositories import MongoCatalogue, _MongoScope

    class DB(SimpleNamespace):
        def __getitem__(self, name):
            return getattr(self, name)

    session = object()
    database = DB(
        **{name: Collection(session) for name in ("movies", "copies", "loans", "entity_ids")}
    )
    scope = _MongoScope(database, session, 1, writable=True)
    return database, session, scope, MongoCatalogue(scope)


def test_maximum_transfer_groups_identity_claims_and_validates_the_final_records():
    pytest.importorskip("pymongo")
    database, _, scope, catalogue = boundary()
    movies = [{**MOVIE, "id": uid(index)} for index in range(1, 1001)]
    copies = [
        {**COPY, "id": uid(index + 1001), "movie_id": movies[index % len(movies)]["id"]}
        for index in range(5000)
    ]
    assert (
        catalogue.occupied_import_ids([m["id"] for m in movies] + [c["id"] for c in copies])
        == set()
    )
    catalogue.insert_import(movies, copies)
    scope.validate()
    assert database.movies.inserts == [500, 500]
    assert database.copies.inserts == [500] * 10
    assert database.entity_ids.inserts == [500] * 12
    assert database.entity_ids.reads == [500] * 12
    assert database.movies.reads == [500, 500]
    assert database.copies.reads == database.loans.reads == [500] * 10
    assert not hasattr(database, "users")


@pytest.mark.parametrize("conflict", ["parent_absent", "history_mismatch"])
def test_bulk_insert_still_rejects_missing_parents_and_inconsistent_history(conflict):
    pytest.importorskip("pymongo")
    database, _, scope, catalogue = boundary()
    movie = {**MOVIE, "id": uid(1)}
    copy = {**COPY, "id": uid(2), "movie_id": movie["id"]}
    if conflict == "history_mismatch":
        database.loans.history_conflict = {"_id": uid(3)}
    catalogue.insert_import([movie], [copy])
    if conflict == "parent_absent":
        database.movies.rows.clear()
    with pytest.raises(ValueError, match="Invalid copy id or reference|Copy history"):
        scope.validate()


def test_bulk_prepares_all_bson_before_issuing_the_first_write():
    pytest.importorskip("pymongo")
    database, _, _, catalogue = boundary()
    movie = {**MOVIE, "id": uid(1)}
    malformed_copy = {**COPY, "id": "not a UUID", "movie_id": movie["id"]}
    with pytest.raises(ValueError):
        catalogue.insert_import([movie], [malformed_copy])
    assert database.movies.inserts == database.copies.inserts == database.entity_ids.inserts == []


def test_natural_key_lookup_is_grouped_and_ambiguity_read_is_bounded():
    pytest.importorskip("pymongo")
    from bson import Int64

    from app.persistence.mongo_model import encode_record

    database, session, _, catalogue = boundary()
    movie = {**MOVIE, "id": uid(1)}
    key = (movie["title"].casefold(), movie["year"], movie["director"].casefold())
    keys = [key, *((f"film{i}", 2001, "director") for i in range(100))]
    database.movies.aggregate = Mock(
        side_effect=[
            nullcontext(
                [
                    {
                        "_id": dict(zip(("title", "year", "director"), key, strict=True)),
                        "rows": [
                            encode_record("movies", movie),
                            encode_record("movies", {**movie, "id": uid(2)}),
                        ],
                    }
                ]
            ),
            nullcontext([]),
        ]
    )
    matches = catalogue.import_movie_matches(keys)
    assert [record["id"] for record in matches[key]] == [uid(1), uid(2)]
    assert all(matches[missing] == [] for missing in keys[1:])
    first, second = database.movies.aggregate.call_args_list
    for call, length in ((first, 100), (second, 1)):
        assert call.kwargs == {"session": session}
        selector, grouping = call.args[0]
        assert len(selector["$match"]["$or"]) == length
        assert all(type(item["year"]) is Int64 for item in selector["$match"]["$or"])
        assert grouping["$group"]["rows"]["$firstN"]["n"] == 2
