"""Exercise the real BSON boundary, independently of a MongoDB server."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from bson import BSON, Int64
from mongo_fixtures import related_document

from app.storage_v2 import COLLECTIONS

MOVIE_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "a2222222-2222-4222-8222-222222222222"


def model():
    assert importlib.util.find_spec("app.persistence") is not None, (
        "The persistence package is not implemented"
    )
    assert importlib.util.find_spec("app.persistence.mongo_model") is not None, (
        "The MongoDB document boundary is not implemented"
    )
    from app.persistence import mongo_model

    return mongo_model


def user(timestamp=1794318769):
    return {
        "id": USER_ID,
        "username": "membre",
        "display_name": "Élève 🎬",
        "password_hash": "$argon2id$unchanged-fixture-hash",
        "role": "member",
        "enabled": True,
        "created_at": timestamp,
    }


@pytest.mark.parametrize("timestamp", [0, 1794318769, 2**53, 2**63 - 1])
def test_bson_roundtrip_preserves_exact_seconds_uuid_and_password_hash(timestamp):
    boundary = model()
    source = user(timestamp)
    before = copy.deepcopy(source)
    encoded = boundary.encode_record("users", source)
    wire = BSON.encode(encoded)
    persisted = BSON(wire).decode()
    assert persisted["_id"] == USER_ID
    assert "id" not in persisted
    assert type(persisted["created_at"]) is Int64
    assert type(persisted["enabled"]) is bool
    decoded = boundary.decode_record("users", persisted)
    assert decoded == before == source
    assert type(decoded["created_at"]) is int
    assert decoded["password_hash"] == before["password_hash"]


@pytest.mark.parametrize("bad", [2**63, -(2**63) - 1, 1.5, float("nan")])
def test_unrepresentable_numeric_value_is_refused_before_bson_encoding(bad):
    with pytest.raises(ValueError):
        model().encode_record("users", user(bad))


@pytest.mark.parametrize("bad", [USER_ID.upper(), "not-a-uuid", {"$ne": None}, 123])
def test_identity_is_a_canonical_uuid_string_never_a_query(bad):
    value = user()
    value["id"] = bad
    with pytest.raises(ValueError):
        model().encode_record("users", value)


@pytest.mark.parametrize("reserved", ["_id", "list_name_key", "$where", "a.b"])
def test_reserved_root_keys_are_not_accepted_from_domain_objects(reserved):
    value = user()
    value[reserved] = "injected"
    with pytest.raises(ValueError):
        model().encode_record("users", value)


def test_authentication_counter_uses_its_hmac_identity_and_keeps_bool_distinct():
    source = {"key": "a" * 64, "window_start": 0, "count": 5}
    boundary = model()
    persisted = BSON(BSON.encode(boundary.encode_record("auth_attempts", source))).decode()
    assert persisted["_id"] == source["key"]
    assert "key" not in persisted
    assert boundary.decode_record("auth_attempts", persisted) == source
    assert type(persisted["count"]) is Int64


def test_private_collection_key_uses_python_casefold_and_stays_private():
    boundary = model()
    source = {
        "id": MOVIE_ID,
        "owner_id": USER_ID,
        "movie_id": MOVIE_ID,
        "kind": "custom",
        "list_name": "Straße Σς",
        "supports": [],
        "created_at": 0,
    }
    persisted = BSON(BSON.encode(boundary.encode_record("collections", source))).decode()
    assert persisted["list_name_key"] == "strasse σσ"
    assert boundary.collection_name("collections") == "collection_entries"
    assert boundary.decode_record("collections", persisted) == source
    persisted["list_name_key"] = "wrong"
    with pytest.raises(ValueError):
        boundary.decode_record("collections", persisted)


def test_legal_collection_name_can_expand_threefold_during_casefold():
    source = {
        "id": MOVIE_ID,
        "owner_id": USER_ID,
        "movie_id": MOVIE_ID,
        "kind": "custom",
        "list_name": "ΐ" * 80,
        "supports": [],
        "created_at": 0,
    }
    boundary = model()
    encoded = boundary.encode_record("collections", source)
    assert len(encoded["list_name_key"]) == 240
    assert boundary.decode_record("collections", BSON(BSON.encode(encoded)).decode()) == source
    schema = boundary.collection_schemas()["collection_entries"]
    assert schema["properties"]["list_name_key"]["maxLength"] >= 240


def test_nested_receipt_identifiers_do_not_become_mongo_id_fields():
    value = {
        "id": USER_ID,
        "actor_id": MOVIE_ID,
        "response": {"id": MOVIE_ID, "movie_id": MOVIE_ID, "_etag": '"hash"'},
    }
    boundary = model()
    persisted = boundary.encode_record("idempotency_records", value)
    assert persisted["response"]["id"] == MOVIE_ID
    assert "_id" not in persisted["response"]
    assert boundary.decode_record("idempotency_records", persisted) == value


def test_unicode_lists_and_metadata_large_seconds_survive_the_wire():
    boundary = model()
    value = {
        "id": MOVIE_ID,
        "title": "Échos",
        "year": 2026,
        "director": "Fictif",
        "actors": ["Nom, prénom", "CR\rLF\nNUL\0🎬"],
        "genres": ["Σ"],
        "synopsis": "",
        "external_source": {"qid": "Q1", "revision": 10**15, "retrieved_at": 2**53},
    }
    persisted = BSON(BSON.encode(boundary.encode_record("movies", value))).decode()
    assert boundary.decode_record("movies", persisted) == value
    assert type(persisted["external_source"]["retrieved_at"]) is Int64


def test_movie_search_keys_are_canonical_reserved_hidden_and_checked_on_decode():
    boundary = model()
    value = {
        "id": MOVIE_ID,
        "title": "Straße",
        "year": 2026,
        "director": "Σς",
        "actors": ["Fin", "Début"],
        "genres": ["ΐ" * 80],
        "synopsis": "",
    }
    persisted = boundary.encode_record("movies", value)
    assert persisted["title_key"] == "strasse"
    assert persisted["director_key"] == "σσ"
    assert persisted["search_key"] == "strasse σσ fin début"
    assert len(persisted["genre_keys"][0]) == 240
    assert boundary.decode_record("movies", persisted) == value
    for key in ("title_key", "director_key", "search_key", "genre_keys"):
        with pytest.raises(ValueError):
            boundary.encode_record("movies", {**value, key: persisted[key]})
        bad = {**persisted, key: "wrong"}
        with pytest.raises(ValueError):
            boundary.decode_record("movies", bad)
        absent = {k: v for k, v in persisted.items() if k != key}
        with pytest.raises(ValueError):
            boundary.decode_record("movies", absent)
    schema = boundary.collection_schemas()["movies"]
    assert set(("title_key", "director_key", "search_key", "genre_keys")).issubset(
        schema["required"]
    )
    assert schema["properties"]["genre_keys"]["items"]["maxLength"] >= 240


def test_audit_ordinal_is_explicit_positive_reserved_and_hidden():
    value = {
        "id": USER_ID,
        "actor_id": MOVIE_ID,
        "entity_type": "collection",
        "entity_id": MOVIE_ID,
        "action": "collection.created",
        "timestamp": 0,
    }
    boundary = model()
    with pytest.raises(ValueError):
        boundary.encode_record("audit_events", value)
    for bad in (0, -1, True, 2**63):
        with pytest.raises(ValueError):
            boundary.encode_record("audit_events", value, audit_ordinal=bad)
    encoded = boundary.encode_record("audit_events", value, audit_ordinal=1)
    assert type(encoded["_ordinal"]) is Int64
    assert boundary.decode_record("audit_events", encoded) == value
    with pytest.raises(ValueError):
        boundary.encode_record("audit_events", {**value, "_ordinal": 1}, audit_ordinal=1)
    without = {k: v for k, v in encoded.items() if k != "_ordinal"}
    with pytest.raises(ValueError):
        boundary.decode_record("audit_events", without)


def test_native_schemas_cover_every_entity_and_rename_only_root_identity():
    boundary = model()
    schemas = boundary.collection_schemas()
    assert set(schemas) == {boundary.collection_name(name) for name in COLLECTIONS}
    assert schemas["users"]["properties"]["created_at"]["bsonType"] == "long"
    assert schemas["users"]["properties"]["enabled"]["bsonType"] == "bool"
    assert "id" not in schemas["users"]["properties"]
    assert "_id" in schemas["users"]["required"]
    assert "list_name_key" in schemas["collection_entries"]["required"]
    encoded = json.dumps(schemas)
    for unsupported in ('"$ref"', '"$defs"', '"if"', '"then"', '"else"', '"type"'):
        assert unsupported not in encoded
    receipt = schemas["idempotency_records"]["oneOf"][0]
    assert "_id" in receipt["properties"]
    assert "id" in receipt["properties"]["response"]["properties"]
    schemas["users"]["required"].clear()
    assert boundary.collection_schemas()["users"]["required"]


def test_all_related_entities_survive_real_bson_and_existing_domain_validation():
    from app.storage_v2 import validate_document

    boundary = model()
    source = related_document()
    decoded = {"schema_version": source["schema_version"], "revision": source["revision"]}
    for name in COLLECTIONS:
        decoded[name] = [
            boundary.decode_record(
                name,
                BSON(
                    BSON.encode(
                        boundary.encode_record(
                            name,
                            row,
                            **({"audit_ordinal": position + 1} if name == "audit_events" else {}),
                        )
                    )
                ).decode(),
            )
            for position, row in enumerate(source[name])
        ]
    assert validate_document(decoded) == source


def test_unknown_schema_keyword_fails_closed_instead_of_dropping_validation():
    boundary = model()
    source = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/catalogue-v2.schema.json").read_text()
    )
    source["$defs"]["user"]["unevaluatedProperties"] = False
    with pytest.raises(ValueError):
        boundary.collection_schemas(source)


def test_native_indexes_enforce_conflicting_active_states_not_only_ids():
    specs = model().collection_indexes()
    assert specs["users"][0] == {
        "name": "username_unique",
        "key": [("username", 1)],
        "unique": True,
    }
    loans = {entry["name"]: entry for entry in specs["loans"]}
    assert loans["active_copy_unique"]["key"] == [("copy_id", 1)]
    assert loans["active_copy_unique"]["unique"] is True
    assert loans["active_copy_unique"]["partialFilterExpression"] == {"status": "active"}
    assert loans["open_borrower_movie_unique"]["partialFilterExpression"] == {
        "status": {"$in": ["pending", "active"]}
    }
    reservations = {entry["name"]: entry for entry in specs["reservations"]}
    assert reservations["sequence_unique"]["unique"] is True
    assert reservations["waiting_user_movie_unique"]["partialFilterExpression"] == {
        "status": "waiting"
    }
    assert reservations["fulfilled_loan_unique"]["partialFilterExpression"] == {
        "status": "fulfilled"
    }
    entries = specs["collection_entries"][0]
    assert entries["unique"] is True
    assert entries["key"] == [("owner_id", 1), ("movie_id", 1), ("kind", 1), ("list_name_key", 1)]
    assert specs["audit_events"][0] == {
        "name": "audit_append_order",
        "key": [("_ordinal", 1)],
        "unique": True,
    }
    assert all("expireAfterSeconds" not in entry for values in specs.values() for entry in values)


@pytest.mark.parametrize("name", ["users.$cmd", "$where", "unknown", None])
def test_only_fixed_collection_names_can_be_selected(name):
    with pytest.raises(ValueError):
        model().collection_name(name)
