"""Explicit offline cutover and rollback between JSON v2 and MongoDB."""

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import struct
import sys
from contextlib import ExitStack
from pathlib import Path

import portalocker

from .persistence.errors import CommitOutcomeUnknown, TransactionUnavailable
from .storage_v2 import V2Store, _encode, _write_new, decode_document

URI_ENV = "CINEVAULT_MIGRATION_MONGODB_URI"
BATCH_SIZE = 500


class MigrationError(ValueError):
    """Local precondition failure whose diagnostic omits input values."""


class _AlreadyApplied(Exception):
    """Abort the temporary coordination update when the target already matches."""


def _path(path):
    path = Path(path)
    if os.name == "nt" and (":" in path.name or path.name.endswith((".", " "))):
        raise MigrationError("Migration requires an unambiguous file name")
    candidate = path.parent.resolve(strict=True) / path.name
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return candidate
    _require_regular(info)
    # Resolve existing Windows short names to the same sibling V2Store lock.
    return candidate.resolve(strict=True)


def _file_state(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _require_regular(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise MigrationError("Migration requires a regular file without aliases")


def _read_file(path):
    # Symlink and hardlink aliases can use a different V2Store lock file.
    initial = path.lstat()
    _require_regular(initial)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read()
        closed = os.fstat(stream.fileno())
    latest = path.lstat()
    _require_regular(latest)
    # Windows may expose different ctime semantics through lstat and fstat.
    # Compare ctime only within the same kind of observation.
    if (
        _file_state(initial) != _file_state(opened)
        or _file_state(opened) != _file_state(closed)
        or _file_state(closed) != _file_state(latest)
        or initial.st_ctime_ns != latest.st_ctime_ns
        or opened.st_ctime_ns != closed.st_ctime_ns
        or initial.st_mode != latest.st_mode
        or len(data) != closed.st_size
    ):
        raise MigrationError("Migration input changed during inspection")
    return data


def _require_sha256(expected, actual, message):
    if type(expected) is not str or re.fullmatch("[a-f0-9]{64}", expected) is None:
        raise MigrationError("An inspected SHA-256 digest is required")
    if not hmac.compare_digest(expected, actual):
        raise MigrationError(message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _lock(path):
    return portalocker.Lock(V2Store(path).lock_path, mode="a", timeout=10)


def _lock_path(path):
    return Path(V2Store(path).lock_path)


def _ordered_fields(value):
    if isinstance(value, dict):
        return {key: _ordered_fields(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_ordered_fields(item) for item in value]
    return value


def _snapshot_sha256(snapshot):
    from bson import BSON
    from bson.raw_bson import RawBSONDocument

    digest = hashlib.sha256(b"cinevault-native-p5-v1\0")
    for name, rows in sorted(snapshot.items()):
        digest.update(name.encode("ascii") + b"\0" + struct.pack(">Q", len(rows)))
        parsed = [BSON(row.raw).decode() if type(row) is RawBSONDocument else row for row in rows]
        for row in sorted(parsed, key=lambda entry: entry["_id"]):
            digest.update(BSON.encode(_ordered_fields(row)))
    return digest.hexdigest()


def _receipt(snapshot, revision):
    return {
        "snapshot_sha256": _snapshot_sha256(snapshot),
        "revision": revision,
        "counts": {name: len(rows) for name, rows in snapshot.items()},
    }


def _decode_json(raw):
    try:
        return decode_document(raw)
    except (ValueError, TypeError, AttributeError):
        raise MigrationError("Invalid JSON v2 migration input") from None


def _prepare_forward(raw):
    from .persistence.migration_codec import MigrationDataError, encode_snapshot

    document = _decode_json(raw)
    try:
        snapshot = encode_snapshot(document)
    except MigrationDataError as exc:
        raise MigrationError(str(exc)) from None
    return snapshot, {**_receipt(snapshot, document["revision"]), "source_sha256": _sha(raw)}


def inspect_json(source):
    """Inventory exact bytes without a lock-file creation or database connection."""
    _, report = _prepare_forward(_read_file(_path(source)))
    return {"status": "inspected", **report}


def _require_index_specs(collection, required):
    expected = {"_id_": {"key": [("_id", 1)]}, **{spec["name"]: spec for spec in required}}
    with collection.list_indexes() as cursor:
        entries = list(cursor)
    if len(entries) != len(expected) or {entry["name"] for entry in entries} != set(expected):
        raise MigrationError("MongoDB index inventory differs from the candidate model")
    for entry in entries:
        name = entry["name"]
        wanted = expected[name]
        if (
            set(entry) - {"name", "v", "key", "unique", "partialFilterExpression"}
            or entry.get("v") != 2
            or list(entry["key"].items()) != wanted["key"]
            or entry.get("unique", name == "_id_") != wanted.get("unique", name == "_id_")
            or entry.get("partialFilterExpression") != wanted.get("partialFilterExpression")
        ):
            raise MigrationError("MongoDB index specification differs from the candidate model")


def _preflight(database):
    from pymongo import timeout

    from .persistence.mongo_maintenance import _check_options, auxiliary_schemas
    from .persistence.mongo_model import collection_indexes, collection_schemas

    schemas = {**collection_schemas(), **auxiliary_schemas()}
    indexes = collection_indexes()
    # MongoDB does not allow listCollections/listIndexes inside transactions.
    # The operator must exclude both DDL and external writers throughout.
    with timeout(15):
        with database.list_collections() as cursor:
            entries = list(cursor)
        if len(entries) != len(schemas) or {entry["name"] for entry in entries} != set(schemas):
            raise MigrationError("MongoDB collection inventory differs from the candidate model")
        for entry in entries:
            options = entry.get("options", {})
            if (
                entry["type"] != "collection"
                or set(options) - {"validator", "validationLevel", "validationAction", "collation"}
                or options.get("collation", {"locale": "simple"}) != {"locale": "simple"}
            ):
                raise MigrationError("MongoDB collection options differ from the candidate model")
            try:
                _check_options(options, schemas[entry["name"]])
            except ValueError:
                raise MigrationError("MongoDB validator differs from the candidate model") from None
            _require_index_specs(database[entry["name"]], indexes.get(entry["name"], ()))
    return tuple(schemas)


def _read_snapshot(database, names, session):
    from bson.codec_options import CodecOptions
    from bson.raw_bson import RawBSONDocument

    options = CodecOptions(document_class=RawBSONDocument)
    snapshot = {}
    for name in names:
        collection = database[name].with_options(codec_options=options)
        with collection.find({}, session=session) as cursor:
            snapshot[name] = list(cursor)
    return snapshot


def _decode_snapshot(snapshot):
    from .persistence.migration_codec import MigrationDataError, decode_snapshot

    try:
        return decode_snapshot(snapshot)
    except MigrationDataError as exc:
        raise MigrationError(str(exc)) from None


def _runner(database):
    from .persistence.mongo_transactions import TransactionRunner

    return TransactionRunner(database.client, timeout_seconds=45, max_attempts=3)


def _write_forward(database, session, names, snapshot, report):
    from bson import Int64
    from bson.codec_options import CodecOptions
    from bson.raw_bson import RawBSONDocument
    from pymongo import ReturnDocument

    # Change a value even at Int64.MAX so the first operation really obtains
    # the lock. The temporary bit lasts only inside this transaction.
    coordinate = database["coordination"].with_options(
        codec_options=CodecOptions(document_class=RawBSONDocument)
    )
    original = coordinate.find_one_and_update(
        {"_id": "business", "revision": {"$type": "long", "$gte": Int64(0)}},
        {"$bit": {"revision": {"xor": Int64(1)}}},
        return_document=ReturnDocument.BEFORE,
        session=session,
    )
    if original is None:
        raise MigrationError("MongoDB candidate lacks a valid coordination document")
    current = _read_snapshot(database, names, session)
    if len(current["coordination"]) != 1:
        raise MigrationError("MongoDB candidate lacks a valid coordination document")
    current["coordination"] = [original]
    previous = _decode_snapshot(current)
    if _snapshot_sha256(current) == report["snapshot_sha256"]:
        raise _AlreadyApplied
    if previous["revision"] != 0 or any(
        rows for name, rows in current.items() if name != "coordination"
    ):
        raise MigrationError("MongoDB candidate is neither empty nor exactly the inspected source")
    for name, rows in snapshot.items():
        if name != "coordination":
            for start in range(0, len(rows), BATCH_SIZE):
                database[name].insert_many(
                    rows[start : start + BATCH_SIZE], ordered=True, session=session
                )
    updated = database["coordination"].update_one(
        {"_id": "business"},
        {"$set": {"revision": Int64(report["revision"])}},
        session=session,
    )
    if updated.matched_count != 1:
        raise MigrationError("MongoDB candidate coordination changed during migration")
    written = _read_snapshot(database, names, session)
    _decode_snapshot(written)
    if _snapshot_sha256(written) != report["snapshot_sha256"]:
        raise MigrationError("MongoDB candidate differs after transactional readback")


def apply_forward(database, source, backup, *, expected_source_sha256):
    """Import to an explicitly isolated, separately provisioned candidate."""
    source, backup = _path(source), _path(backup)
    if backup in {source, _lock_path(source)}:
        raise MigrationError("JSON source and retained backup must be separate files")
    with _lock(source):
        raw = _read_file(source)
        _require_sha256(expected_source_sha256, _sha(raw), "JSON source changed since inspection")
        if not hmac.compare_digest(raw, _read_file(backup)):
            raise MigrationError("Retained JSON backup does not match the inspected source")
        snapshot, report = _prepare_forward(raw)
        names = _preflight(database)
        try:
            _runner(database).run(
                lambda session: _write_forward(database, session, names, snapshot, report)
            )
        except _AlreadyApplied:
            return {"status": "already_applied", **report}
        return {"status": "applied", **report}


def _read_verified_snapshot(database):
    names = _preflight(database)

    def read(session):
        snapshot = _read_snapshot(database, names, session)
        document = _decode_snapshot(snapshot)
        return document, _receipt(snapshot, document["revision"])

    return _runner(database).run(read)


def reconcile_forward(database, source, *, expected_source_sha256):
    """Read-only confirmation after an uncertain forward commit."""
    raw = _read_file(_path(source))
    _require_sha256(expected_source_sha256, _sha(raw), "JSON source changed since inspection")
    _, expected = _prepare_forward(raw)
    _, observed = _read_verified_snapshot(database)
    if observed["snapshot_sha256"] != expected["snapshot_sha256"]:
        raise CommitOutcomeUnknown("Read-only reconciliation did not confirm the imported state")
    return {"status": "matched", **expected}


def _prepare_reverse(database):
    document, report = _read_verified_snapshot(database)
    candidate = _encode(_ordered_fields(document))
    return candidate, {**report, "candidate_sha256": _sha(candidate)}


def inspect_mongo(database):
    """Inspect one coherent native snapshot without producing an output file."""
    _, report = _prepare_reverse(database)
    return {"status": "inspected", **report}


def apply_reverse(
    database, original, destination, *, expected_original_sha256, expected_snapshot_sha256
):
    """Export a new JSON file while the operator keeps database writers stopped."""
    original, destination = _path(original), _path(destination)
    if destination in {original, _lock_path(original)}:
        raise MigrationError("JSON recovery destination must differ from the original")
    if _lock_path(destination) == original:
        raise MigrationError("JSON recovery lock would alias the original")
    with ExitStack() as stack:
        for path in sorted((original, destination)):
            stack.enter_context(_lock(path))
        raw = _read_file(original)
        _require_sha256(
            expected_original_sha256, _sha(raw), "Original JSON differs from its trusted digest"
        )
        _decode_json(raw)
        candidate, report = _prepare_reverse(database)
        _require_sha256(
            expected_snapshot_sha256,
            report["snapshot_sha256"],
            "MongoDB source changed since inspection",
        )
        if not destination.exists():
            try:
                _write_new(destination, candidate)
            except FileExistsError:
                pass
            else:
                return {"status": "exported", **report}
        if not hmac.compare_digest(_read_file(destination), candidate):
            raise MigrationError("JSON recovery destination already contains different bytes")
        return {"status": "already_exported", **report}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # An accidental URI in an unknown option must not be echoed.
        self.exit(2, "Invalid migration arguments. Use --help.\n")


def _parser():
    parser = _Parser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "inspect-json",
        "apply-forward",
        "reconcile-forward",
        "inspect-mongo",
        "apply-reverse",
    ):
        command = commands.add_parser(name, allow_abbrev=False)
        if name in {"inspect-json", "apply-forward", "reconcile-forward"}:
            command.add_argument("--source", type=Path, required=True)
        if name in {"apply-forward", "reconcile-forward"}:
            command.add_argument("--expected-source-sha256", required=True)
        if name != "inspect-json":
            command.add_argument("--database", required=True)
        if name in {"apply-forward", "apply-reverse"}:
            command.add_argument(
                "--isolated-and-writers-stopped", action="store_true", required=True
            )
        if name == "apply-forward":
            command.add_argument("--backup", type=Path, required=True)
        if name == "apply-reverse":
            command.add_argument("--original", type=Path, required=True)
            command.add_argument("--destination", type=Path, required=True)
            command.add_argument("--expected-original-sha256", required=True)
            command.add_argument("--expected-snapshot-sha256", required=True)
    return parser


def _online(args):
    from pymongo import MongoClient, ReadPreference
    from pymongo.errors import ConfigurationError, InvalidURI, PyMongoError
    from pymongo.uri_parser import parse_uri

    from .persistence.factory import validate_mongo_uri

    if re.fullmatch("[a-z][a-z0-9_]{0,62}", args.database) is None or args.database in {
        "admin",
        "config",
        "local",
    }:
        raise MigrationError("A named CineVault candidate database is required")
    uri = os.environ.get(URI_ENV)
    if not uri:
        raise MigrationError(f"The {URI_ENV} environment variable is required")
    try:
        validate_mongo_uri(uri, parse_uri)
    except (ConfigurationError, InvalidURI, ValueError, TypeError):
        raise MigrationError("MongoDB connection configuration is invalid") from None
    try:
        with MongoClient(uri, connect=False, serverSelectionTimeoutMS=5000) as client:
            database = client.get_database(args.database, read_preference=ReadPreference.PRIMARY)
            if args.command == "apply-forward":
                return apply_forward(
                    database,
                    args.source,
                    args.backup,
                    expected_source_sha256=args.expected_source_sha256,
                )
            if args.command == "reconcile-forward":
                return reconcile_forward(
                    database, args.source, expected_source_sha256=args.expected_source_sha256
                )
            if args.command == "inspect-mongo":
                return inspect_mongo(database)
            return apply_reverse(
                database,
                args.original,
                args.destination,
                expected_original_sha256=args.expected_original_sha256,
                expected_snapshot_sha256=args.expected_snapshot_sha256,
            )
    except PyMongoError:
        raise TransactionUnavailable("MongoDB migration operation unavailable") from None


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        report = inspect_json(args.source) if args.command == "inspect-json" else _online(args)
    except CommitOutcomeUnknown:
        print(
            "commit_outcome_unknown: confirm matching state in a read-only inspection",
            file=sys.stderr,
        )
        return 3
    except (TransactionUnavailable, OSError, portalocker.exceptions.LockException):
        print("migration_unavailable: database, file access or lock unavailable", file=sys.stderr)
        return 4
    except MigrationError as exc:
        print(f"migration_refused: {exc}", file=sys.stderr)
        return 2
    except (ImportError, ValueError, TypeError, KeyError):
        print(
            "migration_refused: invalid driver configuration or unavailable BSON dependency",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
