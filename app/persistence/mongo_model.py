"""Explicit BSON representation and native per-entity validation specifications.

This module describes persistence, not authorization or cross-document invariants.
It neither opens a connection nor changes a database at import time.
"""

import copy
import json
import re
import uuid
from pathlib import Path

from bson import Int64

from ..catalogue_query import MOVIE_KEYS, movie_keys
from ..storage_v2 import COLLECTIONS

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/catalogue-v2.schema.json"
_TYPE_MAP = {
    "object": "object",
    "array": "array",
    "string": "string",
    "integer": "long",
    "boolean": "bool",
    "null": "null",
}
_SCALAR_KEYWORDS = {
    "additionalProperties",
    "description",
    "title",
    "enum",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "maxItems",
    "minItems",
    "uniqueItems",
}


def collection_name(name):
    """Only application-defined names are accepted, never a request selector."""
    if not isinstance(name, str) or name not in COLLECTIONS:
        raise ValueError("Unknown persistence collection")
    return "collection_entries" if name == "collections" else name


def _identity_field(name):
    collection_name(name)
    return "key" if name == "auth_attempts" else "id"


def _validate_identity(name, value):
    if not isinstance(value, str):
        raise ValueError("Invalid persistence identity")
    if name == "auth_attempts":
        valid = _HASH.fullmatch(value) is not None
    else:
        try:
            valid = str(uuid.UUID(value)) == value
        except ValueError:
            valid = False
    if not valid:
        raise ValueError("Invalid persistence identity")


def _convert(value, *, encode):
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, Int64):
        if not INT64_MIN <= value <= INT64_MAX:
            raise ValueError("Integer outside BSON Int64 range")
        return Int64(value) if encode else int(value)
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Invalid persistence text") from exc
        return value
    if type(value) is list:
        return [_convert(item, encode=encode) for item in value]
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if type(key) is not str or key.startswith("$") or "." in key or "\0" in key:
                raise ValueError("Invalid persistence field")
            result[_convert(key, encode=encode)] = _convert(item, encode=encode)
        return result
    raise ValueError("Unsupported persistence value")


def encode_record(name, record, *, audit_ordinal=None):
    """Create a BSON-safe copy without changing domain values or password hashes."""
    identity = _identity_field(name)
    if type(record) is not dict or identity not in record:
        raise ValueError("Missing persistence identity")
    if {"_id", "_ordinal", "list_name_key", *MOVIE_KEYS}.intersection(record):
        raise ValueError("Reserved persistence field")
    _validate_identity(name, record[identity])
    result = _convert(record, encode=True)
    result["_id"] = result.pop(identity)
    if name == "audit_events":
        if type(audit_ordinal) is not int or not 1 <= audit_ordinal <= INT64_MAX:
            raise ValueError("Audit encoding requires an explicit positive Int64 ordinal")
        result["_ordinal"] = Int64(audit_ordinal)
    elif audit_ordinal is not None:
        raise ValueError("Only audit events have append ordinals")
    if name == "collections":
        if type(result.get("list_name")) is not str:
            raise ValueError("Invalid collection name")
        result["list_name_key"] = result["list_name"].casefold()
    elif name == "movies":
        result.update(movie_keys(record))
    return result


def decode_record(name, record):
    """Remove technical fields and normalize BSON Int64 to exact domain integers."""
    identity = _identity_field(name)
    if type(record) is not dict or "_id" not in record or identity in record:
        raise ValueError("Invalid stored persistence identity")
    _validate_identity(name, record["_id"])
    result = _convert(record, encode=False)
    result[identity] = result.pop("_id")
    if name == "audit_events":
        if type(record.get("_ordinal")) is not Int64 or not 1 <= record["_ordinal"] <= INT64_MAX:
            raise ValueError("Invalid stored audit ordinal")
        result.pop("_ordinal")
    elif "_ordinal" in result:
        raise ValueError("Unexpected stored audit ordinal")
    if name == "movies":
        expected = movie_keys(result)
        if any(key not in result or result[key] != value for key, value in expected.items()):
            raise ValueError("Invalid stored movie search key")
        for key in expected:
            result.pop(key)
    elif MOVIE_KEYS.intersection(result):
        raise ValueError("Unexpected stored movie search key")
    if name == "collections":
        normalized = result.pop("list_name_key", None)
        if type(result.get("list_name")) is not str or normalized != result["list_name"].casefold():
            raise ValueError("Invalid stored collection key")
    elif "list_name_key" in result:
        raise ValueError("Unexpected stored collection key")
    return result


def _compile(node, definitions, identity, *, root=False, name_key=False):
    if not isinstance(node, dict):
        raise ValueError("Unsupported JSON schema shape")
    if "$ref" in node:
        reference = node["$ref"]
        if (
            not isinstance(reference, str)
            or not reference.startswith("#/$defs/")
            or reference[8:] not in definitions
            or len(node) != 1
        ):
            raise ValueError("Unsupported JSON schema reference")
        return _compile(
            definitions[reference[8:]], definitions, identity, root=root, name_key=name_key
        )
    result = {}
    for key, value in node.items():
        if key in ("if", "then", "else"):
            continue
        if key in _SCALAR_KEYWORDS:
            result[key] = copy.deepcopy(value)
        elif key == "type":
            try:
                result["bsonType"] = (
                    [_TYPE_MAP[item] for item in value]
                    if isinstance(value, list)
                    else _TYPE_MAP[value]
                )
            except (KeyError, TypeError) as exc:
                raise ValueError("Unsupported JSON schema type") from exc
            if value == "integer":
                result["minimum"] = max(node.get("minimum", INT64_MIN), INT64_MIN)
                result["maximum"] = min(node.get("maximum", INT64_MAX), INT64_MAX)
        elif key == "const":
            result["enum"] = [copy.deepcopy(value)]
        elif key == "properties":
            result[key] = {
                ("_id" if root and field == identity else field): _compile(
                    schema, definitions, identity
                )
                for field, schema in value.items()
            }
        elif key == "required":
            result[key] = ["_id" if root and field == identity else field for field in value]
        elif key in ("allOf", "anyOf", "oneOf"):
            result[key] = [
                _compile(schema, definitions, identity, root=root, name_key=name_key)
                for schema in value
            ]
        elif key in ("items", "not"):
            result[key] = _compile(value, definitions, identity)
        else:
            raise ValueError("Unsupported JSON schema keyword")
    if "then" in node or "else" in node:
        if "if" not in node:
            raise ValueError("Conditional schema has no condition")
    if "if" in node:
        condition = _compile(node["if"], definitions, identity, root=root)
        clauses = result.setdefault("allOf", [])
        if "then" in node:
            clauses.append(
                {
                    "anyOf": [
                        {"not": condition},
                        _compile(node["then"], definitions, identity, root=root),
                    ]
                }
            )
        if "else" in node:
            clauses.append(
                {"anyOf": [condition, _compile(node["else"], definitions, identity, root=root)]}
            )
    if root and name_key and node.get("additionalProperties") is False:
        # Case folding may expand one Unicode character into three characters.
        result["properties"]["list_name_key"] = {"bsonType": "string", "maxLength": 240}
        result["required"].append("list_name_key")
    return result


def collection_schemas(source=None):
    """Compile fresh native schemas; unsupported schema changes fail closed."""
    if source is None:
        source = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    schemas = {
        collection_name(name): _compile(
            source["properties"][name]["items"],
            source["$defs"],
            _identity_field(name),
            root=True,
            name_key=name == "collections",
        )
        for name in COLLECTIONS
    }
    schemas["movies"]["properties"].update(
        {
            "title_key": {"bsonType": "string", "minLength": 1, "maxLength": 600},
            "director_key": {"bsonType": "string", "minLength": 1, "maxLength": 360},
            "search_key": {"bsonType": "string", "minLength": 3, "maxLength": 11853},
            "genre_keys": {
                "bsonType": "array",
                "maxItems": 30,
                "items": {"bsonType": "string", "minLength": 1, "maxLength": 240},
            },
        }
    )
    schemas["movies"]["required"].extend(sorted(MOVIE_KEYS))
    schemas["audit_events"]["properties"]["_ordinal"] = {
        "bsonType": "long",
        "minimum": 1,
        "maximum": INT64_MAX,
    }
    schemas["audit_events"]["required"].append("_ordinal")
    return schemas


def collection_indexes():
    """Maintenance specifications; installing these is not an application privilege."""

    def index(name, fields, *, unique=False, partial=None):
        result = {"name": name, "key": [(field, 1) for field in fields]}
        if unique:
            result["unique"] = True
        if partial is not None:
            result["partialFilterExpression"] = partial
        return result

    result = {collection_name(name): [] for name in COLLECTIONS}
    result["users"] = [index("username_unique", ["username"], unique=True)]
    result["sessions"] = [
        index("token_hash_unique", ["token_hash"], unique=True),
        index("user_sessions", ["user_id", "expires_at"]),
    ]
    result["copies"] = [index("movie_copies", ["movie_id", "_id"])]
    result["movies"] = [
        index("movie_title_order", ["title_key", "_id"]),
        index("movie_year_order", ["year", "_id"]),
        index("movie_natural_key", ["year", "title_key", "director_key"]),
    ]
    result["collection_entries"] = [
        index("owner_entry_unique", ["owner_id", "movie_id", "kind", "list_name_key"], unique=True),
        index("owner_entries", ["owner_id", "created_at", "_id"]),
        index("movie_references", ["movie_id"]),
    ]
    result["loans"] = [
        index("active_copy_unique", ["copy_id"], unique=True, partial={"status": "active"}),
        index(
            "open_borrower_movie_unique",
            ["borrower_id", "movie_id"],
            unique=True,
            partial={"status": {"$in": ["pending", "active"]}},
        ),
        index("borrower_loans", ["borrower_id", "status", "requested_at", "_id"]),
        index("loan_time_order", ["requested_at", "_id"]),
        index("borrower_time_order", ["borrower_id", "requested_at", "_id"]),
        index("movie_loans", ["movie_id", "status", "_id"]),
        index("copy_history", ["copy_id", "_id"]),
    ]
    result["reservations"] = [
        index("sequence_unique", ["sequence"], unique=True),
        index(
            "waiting_user_movie_unique",
            ["user_id", "movie_id"],
            unique=True,
            partial={"status": "waiting"},
        ),
        index("fulfilled_loan_unique", ["loan_id"], unique=True, partial={"status": "fulfilled"}),
        index("movie_queue", ["movie_id", "status", "sequence"]),
        index("user_reservations", ["user_id", "status", "sequence"]),
        index("user_reservation_order", ["user_id", "sequence"]),
        index("waiting_expiration", ["status", "expires_at"]),
    ]
    result["idempotency_records"] = [
        index("actor_receipt_unique", ["actor_id", "key_hash"], unique=True),
        index("receipt_expiration", ["expires_at"]),
    ]
    result["audit_events"] = [
        index("audit_append_order", ["_ordinal"], unique=True),
        index("entity_audit", ["entity_type", "entity_id", "timestamp"]),
    ]
    return result
