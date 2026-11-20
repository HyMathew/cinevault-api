"""Operator migration against real files and a strict transaction double."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid

import portalocker
import pytest
from bson import BSON, SON, Int64
from bson.raw_bson import RawBSONDocument
from pymongo import ReturnDocument
from pymongo.errors import OperationFailure

from app.migrate_backends import (
    MigrationError,
    apply_forward,
    apply_reverse,
    inspect_json,
    inspect_mongo,
    main,
    reconcile_forward,
)
from app.persistence.errors import CommitOutcomeUnknown, TransactionUnavailable
from app.persistence.migration_codec import encode_snapshot
from app.persistence.mongo_maintenance import auxiliary_schemas
from app.persistence.mongo_model import collection_indexes, collection_schemas
from app.storage_v2 import V2Store, _encode, empty_document
from tests.mongo_fixtures import related_document


class Cursor(list):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Session:
    def __init__(self, db):
        self.db = db
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        assert not self.in_transaction
        self.db.active = False

    def start_transaction(self, *, read_concern, write_concern, read_preference):
        assert read_concern.document == {"level": "snapshot"}
        assert write_concern.document["w"] == "majority"
        assert read_preference.name == "Primary"
        self.db.trace.append(("begin",))
        self.data = copy.deepcopy(self.db.data)
        self.in_transaction = True

    def commit_transaction(self):
        self.db.trace.append(("commit",))
        self.db.data = self.data
        self.in_transaction = False
        if self.db.lose_commit:
            self.db.lose_commit = False
            raise OperationFailure(
                "synthetic ACK loss", 50, {"errorLabels": ["UnknownTransactionCommitResult"]}
            )

    def abort_transaction(self):
        self.db.trace.append(("abort",))
        self.in_transaction = False


class Collection:
    def __init__(self, db, name, raw=False):
        self.db, self.name, self.raw = db, name, raw

    def with_options(self, *, codec_options):
        assert codec_options.document_class is RawBSONDocument
        return Collection(self.db, self.name, raw=True)

    def list_indexes(self):
        assert not self.db.active
        return Cursor(self.db.indexes[self.name])

    def _rows(self, session):
        assert session.in_transaction and session.db is self.db
        return session.data[self.name]

    def _native(self, row):
        data = BSON.encode(row)
        return RawBSONDocument(data) if self.raw else data.decode()

    def find(self, filter, *, session):
        assert filter == {}
        assert self.raw
        self.db.trace.append(("find", self.name))
        return Cursor(self._native(row) for row in self._rows(session))

    def find_one_and_update(self, filter, update, *, return_document, session):
        assert self.name == "coordination"
        assert filter == {"_id": "business", "revision": {"$type": "long", "$gte": 0}}
        assert type(filter["revision"]["$gte"]) is Int64
        assert type(update["$bit"]["revision"]["xor"]) is Int64
        assert update == {"$bit": {"revision": {"xor": Int64(1)}}}
        assert return_document is ReturnDocument.BEFORE
        assert self.db.trace[-1] == ("begin",)
        self.db.trace.append(("lock", self.name))
        rows = self._rows(session)
        if not rows or type(rows[0].get("revision")) is not Int64:
            return None
        before = self._native(rows[0])
        rows[0]["revision"] = Int64(rows[0]["revision"] ^ 1)
        return before

    def insert_many(self, rows, *, ordered, session):
        assert self.name != "coordination"
        assert ordered is True
        self.db.trace.append(("insert", self.name, len(rows)))
        target = self._rows(session)
        target.extend(copy.deepcopy(rows))
        if self.name == self.db.fail_collection:
            raise OperationFailure("synthetic failure after staged insertion", 121)

    def update_one(self, filter, update, *, session):
        assert self.name == "coordination"
        rows = self._rows(session)
        assert filter == {"_id": "business"}
        assert type(update["$set"]["revision"]) is Int64
        self.db.trace.append(("final_revision", self.name))
        rows[0].update(update["$set"])
        return type("Result", (), {"matched_count": 1})()


class Database:
    def __init__(self, initial):
        self.data = encode_snapshot(initial)
        self.schema = [
            {
                "name": name,
                "type": "collection",
                "options": {
                    "validator": {"$jsonSchema": schema},
                    "validationLevel": "strict",
                    "validationAction": "error",
                },
            }
            for name, schema in {**collection_schemas(), **auxiliary_schemas()}.items()
        ]
        model = collection_indexes()
        self.indexes = {
            entry["name"]: [
                {"v": 2, "name": "_id_", "key": SON([("_id", 1)])},
                *(
                    {**index, "v": 2, "key": SON(index["key"])}
                    for index in model.get(entry["name"], ())
                ),
            ]
            for entry in self.schema
        }
        self.active = False
        self.trace = []
        self.client = self
        self.lose_commit = False
        self.fail_collection = None
        self.on_metadata = lambda: None

    def list_collections(self):
        assert not self.active
        self.trace.append(("metadata",))
        self.on_metadata()
        return Cursor(self.schema)

    def __getitem__(self, name):
        assert name in self.data
        return Collection(self, name)

    def start_session(self):
        assert not self.active
        self.active = True
        return Session(self)


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "original.json"
    backup = tmp_path / "backup.json"
    document = related_document()
    source.write_bytes(_encode(document))
    digest = V2Store(source).snapshot(backup)
    return source, backup, digest, document


def forward(database, inputs):
    source, backup, digest, _ = inputs
    return apply_forward(database, source, backup, expected_source_sha256=digest)


def test_inspect_json_cli_never_writes_or_needs_mongo_uri(tmp_path):
    source = tmp_path / "source.json"
    raw = _encode(related_document())
    source.write_bytes(raw)
    env = {**os.environ}
    env.pop("CINEVAULT_MIGRATION_MONGODB_URI", None)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "app.migrate_backends",
            "inspect-json",
            "--source",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
        env=env,
    )
    report = json.loads(result.stdout)
    assert report["status"] == "inspected"
    assert report["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["counts"]["entity_ids"] == 9
    assert "password_hash" not in result.stdout
    assert list(tmp_path.iterdir()) == [source]


def test_forward_real_lock_same_session_and_verified_replay(inputs):
    source, backup, _, document = inputs
    database = Database(empty_document())

    def verify_lock():
        with pytest.raises(portalocker.exceptions.LockException):
            with portalocker.Lock(V2Store(source).lock_path, mode="a", timeout=0):
                pytest.fail("source lock was not held by the migration")

    database.on_metadata = verify_lock
    assert forward(database, inputs)["status"] == "applied"
    assert database.data == encode_snapshot(document)
    assert database.trace[:3] == [("metadata",), ("begin",), ("lock", "coordination")]
    assert database.trace[-1] == ("commit",)
    database.trace.clear()
    assert forward(database, inputs)["status"] == "already_applied"
    assert database.trace[-1] == ("abort",)
    assert not any(step[0] in {"insert", "final_revision", "commit"} for step in database.trace)
    assert source.read_bytes() == backup.read_bytes()


@pytest.mark.parametrize("revision", (0, 2**63 - 1))
def test_forward_commits_exact_boundary_revision_and_replay_is_idempotent(tmp_path, revision):
    source = tmp_path / "original.json"
    backup = tmp_path / "backup.json"
    document = related_document()
    document["reservations"] = []
    document["revision"] = revision
    source.write_bytes(_encode(document))
    expected = V2Store(source).snapshot(backup)
    database = Database(empty_document())
    args = database, source, backup
    assert apply_forward(*args, expected_source_sha256=expected)["status"] == "applied"
    assert database.data == encode_snapshot(document)
    assert type(database.data["coordination"][0]["revision"]) is Int64
    assert apply_forward(*args, expected_source_sha256=expected)["status"] == "already_applied"
    assert database.data["coordination"][0]["revision"] == revision


def test_empty_source_to_provisioned_zero_is_already_applied(tmp_path):
    source = tmp_path / "empty.json"
    backup = tmp_path / "backup.json"
    V2Store(source).initialize()
    expected = V2Store(source).snapshot(backup)
    database = Database(empty_document())
    result = apply_forward(database, source, backup, expected_source_sha256=expected)
    assert result["status"] == "already_applied"
    assert database.data == encode_snapshot(empty_document())
    assert database.trace[-1] == ("abort",)


@pytest.mark.parametrize("failure", ("changed-source", "different-backup", "same-backup"))
def test_forward_digest_and_backup_guards_precede_mongo(inputs, failure):
    source, backup, digest, _ = inputs
    if failure == "changed-source":
        source.write_bytes(source.read_bytes() + b" ")
    elif failure == "different-backup":
        backup.write_bytes(backup.read_bytes() + b" ")
    else:
        backup = source
    database = Database(empty_document())
    with pytest.raises(MigrationError):
        apply_forward(database, source, backup, expected_source_sha256=digest)
    assert not database.trace


def test_hardlinked_source_is_refused_without_touching_either_file(inputs, tmp_path):
    source, _, _, _ = inputs
    alias = tmp_path / "source-alias.json"
    os.link(source, alias)
    before = source.read_bytes()
    with pytest.raises(MigrationError, match="aliases"):
        inspect_json(alias)
    assert source.read_bytes() == alias.read_bytes() == before


@pytest.mark.parametrize(
    "defect", ("collection", "validator", "sparse", "missing-index", "extra-index")
)
def test_forward_preflight_rejects_metadata_drift_before_lock_write(inputs, defect):
    database = Database(empty_document())
    if defect == "collection":
        database.schema.pop()
    if defect == "validator":
        database.schema[0]["options"]["validationAction"] = "warn"
    if defect == "sparse":
        database.indexes["users"][1]["sparse"] = True
    if defect == "missing-index":
        database.indexes["users"].pop()
    if defect == "extra-index":
        database.indexes["users"].append({"v": 2, "name": "extra", "key": SON([("username", 1)])})
    with pytest.raises(MigrationError):
        forward(database, inputs)
    assert database.trace == [("metadata",)]


@pytest.mark.parametrize("kind", ("valid-different", "partial", "empty-nonzero"))
def test_forward_refuses_different_state_without_committed_writes(inputs, kind):
    _, _, _, document = inputs
    database = Database(document if kind != "empty-nonzero" else empty_document())
    if kind == "valid-different":
        database.data["users"][0]["display_name"] = "Different"
    elif kind == "partial":
        database.data["entity_ids"].pop()
    else:
        database.data["coordination"][0]["revision"] = Int64(8)
    before = copy.deepcopy(database.data)
    with pytest.raises(MigrationError):
        forward(database, inputs)
    assert database.data == before
    assert database.trace[-1] == ("abort",)
    assert not any(step[0] in {"insert", "commit"} for step in database.trace)


def test_forward_rolls_back_late_batch_failure(inputs):
    database = Database(empty_document())
    database.fail_collection = "entity_ids"
    before = copy.deepcopy(database.data)
    with pytest.raises(TransactionUnavailable):
        forward(database, inputs)
    assert database.data == before
    assert any(step[0] == "insert" for step in database.trace)
    assert database.trace[-1] == ("abort",)


def test_forward_chunks_records_and_registry_inside_one_commit(tmp_path):
    source, backup = tmp_path / "original.json", tmp_path / "backup.json"
    document = related_document()
    template = document["copies"][0]
    document["copies"].extend(
        {**template, "id": str(uuid.UUID(int=index))} for index in range(10000, 10502)
    )
    source.write_bytes(_encode(document))
    expected = V2Store(source).snapshot(backup)
    database = Database(empty_document())
    result = apply_forward(database, source, backup, expected_source_sha256=expected)
    inserts = [step[2] for step in database.trace if step[:2] == ("insert", "copies")]
    assert inserts == [500, 3]
    assert result["counts"]["copies"] == 503 and result["counts"]["entity_ids"] == 511
    assert database.trace.count(("commit",)) == database.trace.count(("begin",)) == 1
    assert database.data == encode_snapshot(document)


def test_uncertain_commit_requires_read_only_reconciliation(inputs):
    source, _, digest, document = inputs
    database = Database(empty_document())
    database.lose_commit = True
    with pytest.raises(CommitOutcomeUnknown):
        forward(database, inputs)
    assert database.data == encode_snapshot(document)
    assert database.trace.count(("lock", "coordination")) == 1
    database.trace.clear()
    assert reconcile_forward(database, source, expected_source_sha256=digest)["status"] == "matched"
    assert not any(step[0] in {"lock", "insert", "final_revision"} for step in database.trace)
    database.data["users"][0]["display_name"] = "A new change"
    with pytest.raises(CommitOutcomeUnknown):
        reconcile_forward(database, source, expected_source_sha256=digest)


def test_reverse_current_snapshot_new_path_and_replay(inputs, tmp_path):
    source, _, original_sha, document = inputs
    database = Database(document)
    database.data["coordination"][0]["revision"] = Int64(2)
    database.data["users"][0]["display_name"] = "Nouvel état"
    database.data["audit_events"][0]["_ordinal"] = Int64(127)
    result = inspect_mongo(database)
    path = tmp_path / "recovered.json"
    options = {
        "expected_snapshot_sha256": result["snapshot_sha256"],
        "expected_original_sha256": original_sha,
    }
    assert apply_reverse(database, source, path, **options)["status"] == "exported"
    recovered = V2Store(path).read()
    assert recovered["revision"] == 2 and recovered["users"][0]["display_name"] == "Nouvel état"
    assert apply_reverse(database, source, path, **options)["status"] == "already_exported"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["candidate_sha256"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha
    assert not any(step[0] in {"lock", "insert", "final_revision"} for step in database.trace)
    options["expected_snapshot_sha256"] = "0" * 64
    with pytest.raises(MigrationError):
        apply_reverse(database, source, tmp_path / "stale.json", **options)
    assert not (tmp_path / "stale.json").exists()


def test_reverse_refuses_original_and_existing_unrelated_file(inputs, tmp_path):
    source, _, original_sha, document = inputs
    database = Database(document)
    snapshot = inspect_mongo(database)
    options = {
        "expected_snapshot_sha256": snapshot["snapshot_sha256"],
        "expected_original_sha256": original_sha,
    }
    with pytest.raises(MigrationError):
        apply_reverse(database, source, source, **options)
    lock = source.with_name(source.name + ".lock")
    with pytest.raises(MigrationError):
        apply_reverse(database, source, lock, **options)
    path = tmp_path / "existing.json"
    path.write_bytes(b"keep existing")
    with pytest.raises(MigrationError):
        apply_reverse(database, source, path, **options)
    assert path.read_bytes() == b"keep existing"


def test_reverse_refuses_file_created_after_snapshot_without_overwrite(
    inputs, tmp_path, monkeypatch
):
    from app import migrate_backends

    source, _, original_sha, document = inputs
    database = Database(document)
    snapshot = inspect_mongo(database)
    path = tmp_path / "raced.json"
    write_new = migrate_backends._write_new

    def create_competing_file(destination, data):
        destination.write_bytes(b"competing bytes")
        write_new(destination, data)

    monkeypatch.setattr(migrate_backends, "_write_new", create_competing_file)
    with pytest.raises(MigrationError, match="different bytes"):
        apply_reverse(
            database,
            source,
            path,
            expected_snapshot_sha256=snapshot["snapshot_sha256"],
            expected_original_sha256=original_sha,
        )
    assert path.read_bytes() == b"competing bytes"
    assert not list(tmp_path.glob(".cinevault-v2-*"))


def test_reverse_snapshot_digest_includes_absolute_ordinal_even_at_same_rank(inputs, tmp_path):
    source, _, original_sha, document = inputs
    database = Database(document)
    snapshot = inspect_mongo(database)
    database.data["audit_events"][0]["_ordinal"] = Int64(3)
    after = inspect_mongo(database)
    assert after["candidate_sha256"] == snapshot["candidate_sha256"]
    assert after["snapshot_sha256"] != snapshot["snapshot_sha256"]
    path = tmp_path / "stale.json"
    with pytest.raises(MigrationError, match="MongoDB source changed"):
        apply_reverse(
            database,
            source,
            path,
            expected_snapshot_sha256=snapshot["snapshot_sha256"],
            expected_original_sha256=original_sha,
        )
    assert not path.exists()


def test_cli_does_not_echo_args_or_secret_on_errors(capsys, monkeypatch):
    secret = "mongodb://operator:synthetic-private@invalid.example"
    with pytest.raises(SystemExit) as wrong:
        main(["inspect-mongo", "--database", "isolated_migration", "--uri", secret])
    assert wrong.value.code == 2
    assert secret not in capsys.readouterr().err
    monkeypatch.delenv("CINEVAULT_MIGRATION_MONGODB_URI", raising=False)
    assert main(["inspect-mongo", "--database", "isolated_migration"]) != 0
    assert capsys.readouterr().out == ""


def test_cli_keeps_uncertain_commit_separate_and_requires_writer_stop(monkeypatch, capsys):
    from app import migrate_backends

    calls = []

    def unknown(args):
        calls.append(args)
        raise CommitOutcomeUnknown("synthetic secret-like driver detail")

    monkeypatch.setattr(migrate_backends, "_online", unknown)
    args = [
        "apply-forward",
        "--database",
        "isolated_migration",
        "--source",
        "original.json",
        "--backup",
        "backup.json",
        "--expected-source-sha256",
        "a" * 64,
    ]
    with pytest.raises(SystemExit) as missing:
        main(args)
    assert missing.value.code == 2
    assert calls == []
    capsys.readouterr()
    assert main([*args, "--isolated-and-writers-stopped"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("commit_outcome_unknown:")
    assert "driver detail" not in captured.err


def test_json_inspection_rejects_duplicate_fields_without_creating_lock(tmp_path):
    source = tmp_path / "invalid.json"
    source.write_bytes(b'{"revision":1,"revision":2}')
    with pytest.raises(MigrationError):
        inspect_json(source)
    assert list(tmp_path.iterdir()) == [source]


def test_cli_refuses_insecure_uri_before_opening_a_client(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid URI opened a client")

    monkeypatch.setattr("pymongo.MongoClient", forbidden)
    monkeypatch.setenv(
        "CINEVAULT_MIGRATION_MONGODB_URI",
        "mongodb://operator:synthetic-private@remote.invalid/"
        "?authSource=admin&authMechanism=SCRAM-SHA-256&replicaSet=cv",
    )
    assert main(["inspect-mongo", "--database", "isolated_migration"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "MongoDB connection configuration is invalid" in output.err
    assert "synthetic-private" not in output.err
