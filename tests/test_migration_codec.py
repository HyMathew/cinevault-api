"""Migration conversion contracts without opening a database or writing a file."""

import copy
import struct
import uuid

import pytest
from bson import BSON, Int64
from bson.raw_bson import RawBSONDocument

from app.persistence.migration_codec import MigrationDataError, decode_snapshot, encode_snapshot
from app.receipt_storage import digest
from app.storage_v2 import COLLECTIONS, empty_document
from tests.mongo_fixtures import related_document


def identity(n):
    return str(uuid.UUID(int=n))


def through_bson(snapshot, *, raw=False):
    return {
        name: [
            RawBSONDocument(BSON.encode(row)) if raw else BSON.encode(row).decode() for row in rows
        ]
        for name, rows in snapshot.items()
    }


@pytest.mark.parametrize("raw", (False, True))
def test_full_snapshot_reuses_business_codec_and_survives_real_bson(raw):
    source = related_document()
    source["revision"] = 2**63 - 1
    source["users"][0]["created_at"] = 2**53 + 1
    before = copy.deepcopy(source)
    native = encode_snapshot(source)

    assert set(native) == {
        *({name for name in COLLECTIONS} - {"collections"}),
        "collection_entries",
        "coordination",
        "entity_ids",
    }
    assert native["coordination"] == [{"_id": "business", "revision": Int64(2**63 - 1)}]
    assert type(native["coordination"][0]["revision"]) is Int64
    assert type(native["users"][0]["created_at"]) is Int64
    assert native["auth_attempts"][0]["_id"] == source["auth_attempts"][0]["key"]
    assert native["movies"][0]["title_key"] == "échos fictifs"
    assert native["collection_entries"][0]["list_name_key"] == "strasse σσ"
    assert {row["_id"]: row["kind"] for row in native["entity_ids"]} == {
        row["id"]: kind for kind in COLLECTIONS if kind != "auth_attempts" for row in source[kind]
    }
    snapshot = through_bson(native, raw=raw)
    decoded = decode_snapshot(snapshot)
    assert decoded == before
    assert type(decoded["users"][0]["created_at"]) is int
    assert type(decoded["revision"]) is int
    assert source == before
    assert encode_snapshot(decoded) == native


def test_empty_snapshot_has_exact_auxiliary_shape():
    native = encode_snapshot(empty_document())
    assert native["entity_ids"] == []
    assert native["coordination"][0]["revision"] == 0
    assert len(native) == 12
    assert decode_snapshot(through_bson(native)) == empty_document()


def test_deterministic_identity_order_and_explicit_audit_order():
    source = related_document()
    source["movies"].insert(
        0, {**source["movies"][0], "id": identity(81), "title": "Deuxième film fictif"}
    )
    source["copies"].insert(0, {**source["copies"][0], "id": identity(82)})
    source["audit_events"].insert(
        0, {**source["audit_events"][0], "id": identity(83), "timestamp": 100}
    )
    native = encode_snapshot(source)
    assert [str(row["_ordinal"]) for row in native["audit_events"]] == ["1", "2"]
    assert [row["_id"] for row in native["audit_events"]][0] == identity(83)
    for rows in native.values():
        rows.reverse()
    native["audit_events"][0]["_ordinal"] = Int64(27)
    native["audit_events"][1]["_ordinal"] = Int64(21)
    reversed_state = decode_snapshot(through_bson(native))
    assert reversed_state["audit_events"] == source["audit_events"]
    for name in ("movies", "copies"):
        assert reversed_state[name] == sorted(source[name], key=lambda row: row["id"])
    assert [row["_ordinal"] for row in encode_snapshot(reversed_state)["audit_events"]] == [1, 2]


def test_inverse_recovers_native_mutations_and_keeps_historical_receipt():
    source = related_document()
    initial = encode_snapshot(source)
    native = through_bson(initial)
    native["coordination"][0]["revision"] = Int64(3)
    native["loans"][0]["status"] = "returned"
    native["loans"][0]["returned_at"] = Int64(86403)
    new_movie = {**native["movies"][0], "_id": identity(90), "year": Int64(2027)}
    native["movies"].append(new_movie)
    native["entity_ids"].append({"_id": identity(90), "kind": "movies"})
    event = {
        "_id": identity(91),
        "_ordinal": Int64(2),
        "actor_id": source["users"][0]["id"],
        "timestamp": Int64(86403),
        "entity_type": "loan",
        "entity_id": source["loans"][0]["id"],
        "action": "loan.returned",
    }
    native["audit_events"].insert(0, event)
    native["entity_ids"].append({"_id": identity(91), "kind": "audit_events"})

    recovered = decode_snapshot(through_bson(native))
    assert recovered["revision"] == 3
    assert recovered["loans"][0]["returned_at"] == 86403
    assert recovered["reservations"] == source["reservations"]
    assert recovered["movies"][1]["id"] == identity(90)
    assert recovered["audit_events"][-1]["id"] == identity(91)
    assert recovered["idempotency_records"] == source["idempotency_records"]
    assert recovered["users"] == source["users"]
    assert source["loans"][0]["status"] == "active"


def test_all_receipt_variants_keep_initial_responses_and_expiration():
    source = related_document()
    actor = source["users"][0]["id"]
    movie = source["movies"][0]["id"]
    created_at = 1
    original_loan = {
        **source["loans"][0],
        "copy_id": None,
        "status": "pending",
        "approved_at": None,
        "due_at": None,
    }
    original_reservation = {
        **source["reservations"][0],
        "status": "waiting",
        "expires_at": created_at + 14 * 86400,
        "closed_at": None,
        "loan_id": None,
    }
    import_result = {
        "operation_id": identity(200),
        "created_movies": 1,
        "created_copies": 1,
        "skipped_movies": 0,
        "skipped_copies": 0,
    }
    for number, (operation, body) in enumerate(
        (
            ("loan.request", original_loan),
            ("reservation.create", original_reservation),
            ("catalog.import", import_result),
        ),
        100,
    ):
        source["idempotency_records"].append(
            {
                "id": identity(number),
                "actor_id": actor,
                "key_hash": digest(number),
                "request_hash": digest({"movie_id": movie}),
                "operation": operation,
                "created_at": created_at,
                "expires_at": created_at + 300,
                "response": body
                if operation == "catalog.import"
                else {**body, "_etag": '"' + digest(body) + '"'},
            }
        )
    current = through_bson(encode_snapshot(source))
    restored = decode_snapshot(current)
    assert restored["idempotency_records"] == source["idempotency_records"]
    assert type(current["idempotency_records"][1]["response"]["requested_at"]) is Int64
    assert type(restored["idempotency_records"][1]["response"]["requested_at"]) is int
    assert restored["loans"][0]["status"] == "active"
    assert restored["reservations"][0]["status"] == "fulfilled"


@pytest.mark.parametrize(
    "change",
    (
        lambda d: d.update(revision=2**63),
        lambda d: d["users"][0].update(created_at=True),
        lambda d: d["users"][0].update(created_at=2**63),
        lambda d: d["movies"][0].update(id=d["users"][0]["id"]),
        lambda d: d["copies"][0].update(movie_id=identity(90)),
        lambda d: d["reservations"][0].update(loan_id=identity(90)),
    ),
)
def test_invalid_json_source_fails_closed(change):
    source = related_document()
    change(source)
    with pytest.raises(MigrationDataError):
        encode_snapshot(source)


@pytest.mark.parametrize(
    "change",
    (
        lambda b: b.pop("entity_ids"),
        lambda b: b.update(unexpected=[]),
        lambda b: b["coordination"].append(b["coordination"][0]),
        lambda b: b["coordination"][0].update(revision=1),
        lambda b: b["coordination"][0].update(revision=Int64(-1)),
        lambda b: b["entity_ids"].pop(),
        lambda b: b["entity_ids"].append({"_id": identity(99), "kind": "users"}),
        lambda b: b["entity_ids"].append(b["entity_ids"][0]),
        lambda b: b["entity_ids"][0].update(kind="auth_attempts"),
        lambda b: b["entity_ids"][0].update(extra=True),
        lambda b: b["auth_attempts"].append(b["auth_attempts"][0]),
        lambda b: b["users"][0].update(created_at=0),
        lambda b: b["users"][0].update(enabled=Int64(1)),
        lambda b: b["movies"][0]["external_source"].update(retrieved_at=0),
        lambda b: b["movies"][0].update(title_key="forged"),
        lambda b: b["collection_entries"][0].update(list_name_key="forged"),
        lambda b: b["audit_events"][0].pop("_ordinal"),
        lambda b: b["audit_events"][0].update(_ordinal=True),
        lambda b: b["audit_events"].append({**b["audit_events"][0], "_id": identity(84)}),
        lambda b: b["loans"][0].update(copy_id=identity(99)),
        lambda b: b["idempotency_records"][0]["response"].update(location="forged"),
    ),
)
def test_invalid_native_state_is_not_normalized_into_valid_json(change):
    native = encode_snapshot(related_document())
    change(native)
    with pytest.raises(MigrationDataError):
        decode_snapshot(native)


def test_raw_bson_duplicate_fields_are_refused_before_they_can_be_collapsed():
    native = encode_snapshot(related_document())
    user = BSON.encode(native["users"][0])
    extra = BSON.encode({"enabled": True})[4:-1]
    raw = struct.pack("<i", len(user) + len(extra)) + user[4:-1] + extra + b"\0"
    assert BSON(raw).decode() == BSON(user).decode()
    native["users"][0] = RawBSONDocument(raw)
    with pytest.raises(MigrationDataError, match="users"):
        decode_snapshot(native)


def test_reverse_refuses_source_above_current_json_capacity():
    source = related_document()
    native = encode_snapshot(source)
    user = native["users"][0]
    for n in range(10000):
        new_id = identity(1000 + n)
        native["users"].append({**user, "_id": new_id, "username": f"member-{n}"})
        native["entity_ids"].append({"_id": new_id, "kind": "users"})
    with pytest.raises(MigrationDataError, match="JSON"):
        decode_snapshot(native)
    assert len(native["users"]) == 10001


def test_invalid_user_diagnostic_does_not_contain_the_supplied_value():
    native = encode_snapshot(related_document())
    native["users"][0]["password_hash"] = object()
    with pytest.raises(MigrationDataError, match="users") as error:
        decode_snapshot(native)
    assert error.value.__suppress_context__
