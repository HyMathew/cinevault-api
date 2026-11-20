"""Pure conversion between a JSON v2 document and a full MongoDB snapshot."""

from operator import itemgetter

from bson import BSON, Int64
from bson.errors import BSONError
from bson.raw_bson import RawBSONDocument

from ..storage_v2 import COLLECTIONS, empty_document, validate_document
from .mongo_model import INT64_MAX, collection_name, decode_record, encode_record

_NATIVE_COLLECTIONS = frozenset(
    {"coordination", "entity_ids", *(collection_name(name) for name in COLLECTIONS)}
)


class MigrationDataError(ValueError):
    """A snapshot violates the migration contract. Messages omit input values."""


def _validate_json(document):
    try:
        validate_document(document)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise MigrationDataError("Snapshot violates the JSON v2 document contract") from None
    if not 0 <= document["revision"] <= INT64_MAX:
        raise MigrationDataError("JSON v2 revision is outside the BSON Int64 range")


def _entities(document):
    return {
        row["id"]: name for name in COLLECTIONS if name != "auth_attempts" for row in document[name]
    }


def encode_snapshot(document):
    """Return the 12 native collections, keyed by actual MongoDB collection name.

    Source audit array position is the append order. Other top-level collections
    are canonicalized by identity. Fields and nested arrays retain their values.
    """
    _validate_json(document)
    snapshot = {
        "coordination": [{"_id": "business", "revision": Int64(document["revision"])}],
        "entity_ids": [
            {"_id": identity, "kind": kind}
            for identity, kind in sorted(_entities(document).items())
        ],
    }
    for name in COLLECTIONS:
        native = collection_name(name)
        rows = []
        for index, row in enumerate(document[name], 1):
            try:
                rows.append(
                    encode_record(
                        name, row, audit_ordinal=index if name == "audit_events" else None
                    )
                )
            except (ValueError, TypeError, AttributeError, RecursionError):
                raise MigrationDataError(
                    f"Invalid JSON v2 record in {native} at row {index}"
                ) from None
        snapshot[native] = rows if name == "audit_events" else sorted(rows, key=itemgetter("_id"))
    return snapshot


def _native_row(row):
    if type(row) is RawBSONDocument:
        # Decoding alone collapses duplicate object keys and ignores BSON array
        # indices. An exact re-encoding rejects representation loss at this edge.
        decoded = BSON(row.raw).decode()
        if BSON.encode(decoded) != row.raw:
            raise ValueError("BSON record does not have a canonical encoding")
        return decoded
    if type(row) is not dict:
        raise ValueError("A native record must be a BSON document")
    return row


def _require_native_integers(value):
    # decode_record also accepts Python int for standalone codec callers. A
    # migration must refuse an int32 recursively, before it looks like a valid
    # JSON integer or is inadvertently repaired by the next forward migration.
    if type(value) is int:
        raise ValueError("Unexpected BSON int32")
    if type(value) is dict:
        for item in value.values():
            _require_native_integers(item)
    elif type(value) is list:
        for item in value:
            _require_native_integers(item)


def _read_native_row(row, collection, index):
    try:
        return _native_row(row)
    except (ValueError, BSONError, TypeError, RecursionError):
        raise MigrationDataError(f"Invalid BSON record in {collection} at row {index}") from None


def _revision(rows):
    if len(rows) == 1:
        coordinate = _read_native_row(rows[0], "coordination", 1)
        if (
            set(coordinate) == {"_id", "revision"}
            and type(coordinate["_id"]) is str
            and coordinate["_id"] == "business"
            and type(coordinate["revision"]) is Int64
            and 0 <= coordinate["revision"] <= INT64_MAX
        ):
            return int(coordinate["revision"])
    raise MigrationDataError("Invalid BSON coordination state")


def _check_registry(rows, expected):
    if len(rows) != len(expected):
        raise MigrationDataError("BSON entity registry cardinality does not match")
    seen = set()
    for index, row in enumerate(rows, 1):
        row = _read_native_row(row, "entity_ids", index)
        if set(row) != {"_id", "kind"}:
            raise MigrationDataError("Invalid BSON entity registry fields")
        identity, kind = row["_id"], row["kind"]
        if (
            type(identity) is not str
            or type(kind) is not str
            or identity in seen
            or expected.get(identity) != kind
        ):
            raise MigrationDataError("BSON entity registry does not match the business records")
        seen.add(identity)


def _decode_collection(name, rows):
    native = collection_name(name)
    ordered = []
    sort_keys = set()
    for index, row in enumerate(rows, 1):
        row = _read_native_row(row, native, index)
        try:
            _require_native_integers(row)
            record = decode_record(name, row)
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
            raise MigrationDataError(f"Invalid BSON record in {native} at row {index}") from None
        key = row["_ordinal"] if name == "audit_events" else row["_id"]
        if key in sort_keys:
            raise MigrationDataError(f"Duplicate BSON identity or append ordinal in {native}")
        sort_keys.add(key)
        ordered.append((key, record))
    return [record for _, record in sorted(ordered, key=itemgetter(0))]


def decode_snapshot(snapshot):
    """Convert an exact native snapshot without repairing, pruning or writing.

    RawBSONDocument rows detect duplicate BSON fields. For already decoded
    dictionaries, that evidence has been lost before this function is called.
    """
    if (
        type(snapshot) is not dict
        or set(snapshot) != _NATIVE_COLLECTIONS
        or any(type(rows) is not list for rows in snapshot.values())
    ):
        raise MigrationDataError("Invalid native snapshot collection set or rows")
    document = empty_document()
    document["revision"] = _revision(snapshot["coordination"])
    for name in COLLECTIONS:
        document[name] = _decode_collection(name, snapshot[collection_name(name)])
    _validate_json(document)
    _check_registry(snapshot["entity_ids"], _entities(document))
    return document
