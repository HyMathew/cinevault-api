"""Representation properties, separately from field/business validation."""

import copy
import datetime

import pytest
from bson import BSON, Int64
from hypothesis import given, settings
from hypothesis import strategies as st

from app.persistence.mongo_model import decode_record, encode_record

from .harness import scenario_fixture
from .metrics import record

MAX = 2**63 - 1
TEXT = st.text(st.characters(blacklist_categories=("Cs",)), max_size=30)


@st.composite
def records(draw):
    name = draw(
        st.sampled_from(
            (
                "movies",
                "users",
                "copies",
                "collections",
                "sessions",
                "auth_attempts",
                "audit_events",
            )
        )
    )
    identity, parent = str(draw(st.uuids())), str(draw(st.uuids()))
    text = draw(st.one_of(TEXT, st.sampled_from(("NUL\0CR\rLF\n🎬", "Straße Σς ΐ", "Nom, prénom"))))
    timestamp = draw(st.integers(0, MAX - 28800))
    values = {
        "movies": {
            "id": identity,
            "title": "T" + text + "T",
            "year": 2026,
            "director": "A" + text + "A",
            "actors": ["A" + text + "A"],
            "genres": ["G" + text + "G"],
            "synopsis": text,
        },
        "users": {
            "id": identity,
            "username": "synthetic-property",
            "display_name": "N" + text + "N",
            "password_hash": "$argon2id$synthetic-property",
            "role": "member",
            "enabled": draw(st.booleans()),
            "created_at": timestamp,
        },
        "copies": {
            "id": identity,
            "movie_id": parent,
            "support": "dvd",
            "condition": "good",
            "location": text,
        },
        "collections": {
            "id": identity,
            "movie_id": parent,
            "owner_id": str(draw(st.uuids())),
            "kind": "custom",
            "list_name": "N" + "".join(c for c in text if ord(c) >= 32) + "N",
            "supports": [],
            "created_at": timestamp,
        },
        "sessions": {
            "id": identity,
            "user_id": parent,
            "token_hash": "a" * 64,
            "created_at": timestamp,
            "last_seen": timestamp,
            "expires_at": timestamp + 28800,
        },
        "auth_attempts": {"key": "b" * 64, "count": 1, "window_start": timestamp},
        "audit_events": {
            "id": identity,
            "actor_id": parent,
            "entity_type": "collection",
            "entity_id": str(draw(st.uuids())),
            "action": "collection.created",
            "timestamp": timestamp,
        },
    }
    return name, values[name], draw(st.integers(1, MAX))


def assert_same_typed(left, right):
    assert type(left) is type(right), "Domain types must survive representation roundtrip"
    if isinstance(left, dict):
        assert left.keys() == right.keys()
        for key in left:
            assert_same_typed(left[key], right[key])
    elif isinstance(left, list):
        assert len(left) == len(right)
        for a, b in zip(left, right, strict=True):
            assert_same_typed(a, b)
    else:
        assert left == right


def assert_bson_scalars(domain, stored):
    if type(domain) is int:
        assert type(stored) is Int64 and int(stored) == domain
    elif isinstance(domain, dict):
        for key, value in domain.items():
            assert_bson_scalars(value, stored[key])
    elif isinstance(domain, list):
        for a, b in zip(domain, stored, strict=True):
            assert_bson_scalars(a, b)
    else:
        assert type(stored) is type(domain) and stored == domain


@settings(max_examples=100, deadline=None)
@given(records())
def test_real_bson_roundtrip_preserves_exact_domain_types(case):
    name, domain, ordinal = case
    original = copy.deepcopy(domain)
    encoded = encode_record(
        name, domain, **({"audit_ordinal": ordinal} if name == "audit_events" else {})
    )
    stored = BSON.encode(encoded).decode()
    identity = "key" if name == "auth_attempts" else "id"
    assert type(stored["_id"]) is str and stored["_id"] == domain[identity]
    normalized = {**stored, identity: stored["_id"]}
    assert_bson_scalars(domain, normalized)
    if name == "movies":
        assert stored["title_key"] == domain["title"].casefold()
        assert stored["director_key"] == domain["director"].casefold()
        assert (
            stored["search_key"]
            == " ".join([domain["title"], domain["director"], *domain["actors"]]).casefold()
        )
        assert stored["genre_keys"] == [value.casefold() for value in domain["genres"]]
    if name == "collections":
        assert stored["list_name_key"] == domain["list_name"].casefold()
    if name == "audit_events":
        assert type(stored["_ordinal"]) is Int64 and stored["_ordinal"] == ordinal
    assert_same_typed(original, decode_record(name, stored))
    assert_same_typed(original, domain)
    record("bson_roundtrip", 1)


@settings(max_examples=25, deadline=None)
@given(st.integers(min_value=MAX + 1, max_value=MAX + 10**20))
def test_integer_representation_outside_int64_is_rejected(value):
    for invalid in (value, -value - 1):
        with pytest.raises(ValueError, match="Int64"):
            encode_record(
                "users", {"id": "00000000-0000-0000-0000-000000000001", "created_at": invalid}
            )
    record("int64_overflow", 2)


@settings(max_examples=25, deadline=None)
@given(
    st.sampled_from(
        (
            "_id",
            "_ordinal",
            "search_key",
            "title_key",
            "director_key",
            "genre_keys",
            "list_name_key",
        )
    ),
    TEXT,
)
def test_reserved_fields_are_rejected_at_codec_boundary(key, value):
    with pytest.raises(ValueError, match="Reserved"):
        encode_record("users", {"id": "00000000-0000-0000-0000-000000000001", key: value})
    record("reserved_keys", 1)


@settings(max_examples=25, deadline=None)
@given(
    st.sampled_from((True, False, 0.5, -0.5, datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)))
)
def test_field_validation_belongs_to_uow_and_rolls_back(value):
    with scenario_fixture() as fixture:
        actor = fixture.actors[0]
        account = fixture.backend.query(lambda work: work.accounts.get(actor["id"]))
        before = fixture.path.read_bytes()
        with pytest.raises(ValueError):
            fixture.backend.run(lambda work: work.accounts.save({**account, "created_at": value}))
        assert fixture.path.read_bytes() == before
        # A bool is supported representation, but invalid for this integer field.
        if type(value) is bool:
            assert (
                type(encode_record("users", {**account, "created_at": value})["created_at"]) is bool
            )
        else:
            with pytest.raises(ValueError, match="Unsupported"):
                encode_record("users", {**account, "created_at": value})
    record("field_validation_uow", 1)


def test_int64_edges_and_ordinal_are_exact_not_bool():
    identity = "00000000-0000-0000-0000-000000000001"
    for value in (-(2**63), 2**63 - 1, 0):
        stored = BSON.encode(encode_record("users", {"id": identity, "created_at": value})).decode()
        assert type(stored["created_at"]) is Int64, "BSON integer must be Int64"
        assert decode_record("users", stored)["created_at"] == value
    for invalid in (None, 0, -1, True, MAX + 1):
        with pytest.raises(ValueError, match="ordinal"):
            encode_record("audit_events", {"id": identity}, audit_ordinal=invalid)
