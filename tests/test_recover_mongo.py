"""Recovery exercises the native migration's transaction contract."""

import copy
import json
import uuid

import pytest
from bson import BSON, Int64
from pymongo.errors import OperationFailure

from app import recover_mongo
from app.auth_storage import ACCOUNT_CAPACITY
from app.enterprise_auth import hasher
from app.migrate_backends import MigrationError, read_verified_snapshot
from app.persistence.errors import CommitOutcomeUnknown, TransactionUnavailable
from app.persistence.migration_codec import encode_snapshot
from app.persistence.mongo_model import INT64_MAX
from app.storage_v2 import empty_document
from tests.mongo_fixtures import related_document
from tests.test_migrate_backends import Database

PASSWORD = "synthetic-recovery-administrator"
WRITES = {"lock", "insert", "final_revision"}


@pytest.fixture
def databases():
    document = related_document()
    document["revision"] = 2
    original_user = document["users"][0]
    original_user["password_hash"] = hasher.hash("synthetic-historical-password")
    second_user = {
        **original_user,
        "id": str(uuid.UUID(int=101)),
        "username": "synthetic-member",
        "role": "member",
    }
    document["users"].append(second_user)
    reservation = document["reservations"][0]
    reservation.update(status="waiting", closed_at=None, loan_id=None)
    document["reservations"].append(
        {
            **reservation,
            "id": str(uuid.UUID(int=100)),
            "user_id": second_user["id"],
            "sequence": 2,
            "created_at": 41,
            "expires_at": 86441,
        }
    )
    source, target = Database(document), Database(empty_document())
    source.name, target.name = "recovery_source", "recovery_candidate"
    return source, target


def apply(source, target, expected):
    return recover_mongo.apply(
        source,
        target,
        "replacement-admin",
        "Recovery administrator",
        PASSWORD,
        expected_source_snapshot_sha256=expected,
        timestamp=86420,
    )


def test_quarantine_and_audit_in_one_transaction_with_source_preserved(databases):
    source, target = databases
    original_bytes = BSON.encode(source.data)
    original, before = read_verified_snapshot(source)
    receipt = apply(source, target, before["snapshot_sha256"])
    assert target.trace.count(("commit",)) == 2  # empty-candidate read and publication
    assert target.trace.count(("lock", "coordination")) == 1
    assert {step[0] for step in source.trace}.isdisjoint(WRITES)
    assert BSON.encode(source.data) == original_bytes

    restored, after = read_verified_snapshot(target)
    assert receipt == {
        "status": "applied",
        "operation_id": receipt["operation_id"],
        "source_snapshot_sha256": before["snapshot_sha256"],
        "target_snapshot_sha256": after["snapshot_sha256"],
        "revision": 3,
        "disabled_users": 2,
        "revoked_sessions": 1,
        "closed_reservations": 2,
    }
    assert not restored["sessions"] and not restored["auth_attempts"]
    for collection in ("movies", "copies", "collections", "loans", "idempotency_records"):
        assert restored[collection] == original[collection]
    (admin,) = (user for user in restored["users"] if user["enabled"])
    assert admin["role"] == "admin" and admin["username"] == "replacement-admin"
    assert hasher.verify(admin["password_hash"], PASSWORD)
    for user in restored["users"]:
        if user != admin:
            assert user["password_hash"] != original["users"][0]["password_hash"]
    assert [row["status"] for row in restored["reservations"]] == ["expired", "cancelled"]
    assert [
        (row["action"], row["actor_id"], row["entity_id"]) for row in restored["audit_events"][1:]
    ] == [
        ("reservation.expired", None, original["reservations"][0]["id"]),
        ("reservation.cancelled", admin["id"], original["reservations"][1]["id"]),
        ("recovery.completed", admin["id"], receipt["operation_id"]),
    ]


def test_invalid_digest_same_database_and_replayed_recovery_cannot_write(databases):
    source, target = databases
    expected = recover_mongo.inspect_source(source)["snapshot_sha256"]
    empty = copy.deepcopy(target.data)
    with pytest.raises(MigrationError, match="changed"):
        apply(source, target, "0" * 64)
    assert target.trace == [] and target.data == empty
    with pytest.raises(MigrationError, match="distinct"):
        apply(source, source, expected)
    assert {step[0] for step in source.trace}.isdisjoint(WRITES)

    apply(source, target, expected)
    published = copy.deepcopy(target.data)
    target.trace.clear()
    with pytest.raises(MigrationError, match="empty"):
        apply(source, target, expected)
    assert target.data == published
    assert {step[0] for step in target.trace}.isdisjoint(WRITES)


def test_native_revision_at_int64_max_cannot_advance(databases):
    source, target = databases
    source.data["coordination"][0]["revision"] = Int64(INT64_MAX)
    expected = recover_mongo.inspect_source(source)["snapshot_sha256"]
    with pytest.raises(MigrationError, match="revision"):
        apply(source, target, expected)
    assert target.trace == []


def test_late_write_failure_aborts_entire_candidate(databases):
    source, target = databases
    expected = recover_mongo.inspect_source(source)["snapshot_sha256"]
    target.fail_collection = "audit_events"
    with pytest.raises(TransactionUnavailable):
        apply(source, target, expected)
    assert target.trace[-1] == ("abort",)
    assert target.data == encode_snapshot(empty_document())


def test_target_filled_concurrently_cannot_be_overwritten(databases, monkeypatch):
    source, target = databases
    expected = recover_mongo.inspect_source(source)["snapshot_sha256"]
    competing_import = encode_snapshot(related_document())
    publish = recover_mongo.publish_native_snapshot

    def publish_after_competitor(database, snapshot, report):
        database.data = copy.deepcopy(competing_import)
        return publish(database, snapshot, report)

    monkeypatch.setattr(recover_mongo, "publish_native_snapshot", publish_after_competitor)
    with pytest.raises(MigrationError, match="neither empty"):
        apply(source, target, expected)
    assert target.trace[-1] == ("abort",)
    assert target.data == competing_import


def test_account_capacity_refuses_before_generating_new_credentials(databases, monkeypatch):
    source, target = databases
    document, _ = read_verified_snapshot(source)
    document["users"] += [
        {
            **document["users"][0],
            "id": str(uuid.UUID(int=1000 + index)),
            "username": f"synthetic-{index}",
        }
        for index in range(ACCOUNT_CAPACITY - len(document["users"]))
    ]
    source.data = encode_snapshot(document)
    expected = recover_mongo.inspect_source(source)["snapshot_sha256"]

    def unexpected_hash(*args, **kwargs):
        pytest.fail("Recovery hashed a password despite the full source")

    monkeypatch.setattr("app.recovery.make_user", unexpected_hash)
    with pytest.raises(MigrationError, match="storage limit"):
        apply(source, target, expected)
    assert {step[0] for step in target.trace}.isdisjoint(WRITES)
    assert target.data == encode_snapshot(empty_document())


def test_uncertain_commit_has_read_only_reconciliation(databases, monkeypatch):
    source, target = databases
    expected = recover_mongo.inspect_source(source)["snapshot_sha256"]
    publish = recover_mongo.publish_native_snapshot

    def publish_without_ack(database, snapshot, report):
        database.lose_commit = True
        return publish(database, snapshot, report)

    monkeypatch.setattr(recover_mongo, "publish_native_snapshot", publish_without_ack)
    with pytest.raises(recover_mongo.RecoveryCommitUnknown) as caught:
        apply(source, target, expected)
    receipt = caught.value.receipt
    assert receipt["status"] == "commit_outcome_unknown"
    target.trace.clear()
    assert (
        recover_mongo.reconcile(
            target, expected_target_snapshot_sha256=receipt["target_snapshot_sha256"]
        )["status"]
        == "matched"
    )
    assert {step[0] for step in target.trace}.isdisjoint(WRITES)
    target.data["users"][0]["display_name"] = "Subsequent change"
    with pytest.raises(CommitOutcomeUnknown):
        recover_mongo.reconcile(
            target, expected_target_snapshot_sha256=receipt["target_snapshot_sha256"]
        )
    assert {step[0] for step in target.trace}.isdisjoint(WRITES)


def test_cli_requires_attestation_and_redacts_inputs(capsys):
    args = [
        "apply",
        "--source-uri-file",
        "source",
        "--source-database",
        "source",
        "--target-uri-file",
        "target",
        "--target-database",
        "target",
        "--expected-source-snapshot-sha256",
        "a" * 64,
    ]
    with pytest.raises(SystemExit) as missing:
        recover_mongo.main(args)
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as unknown:
        recover_mongo.main([*args, "--secret=mongodb://secret"])
    assert unknown.value.code == 2
    with pytest.raises(SystemExit) as invalid_command:
        recover_mongo.main(["mongodb://secret"])
    assert invalid_command.value.code == 2
    assert "mongodb://secret" not in capsys.readouterr().err


def test_cli_refuses_uri_aliases_and_shared_principals_before_connecting(tmp_path, monkeypatch):
    uri = (
        "mongodb://synthetic:password@localhost:27017/"
        "?authSource=admin&authMechanism=SCRAM-SHA-256&replicaSet=lab&directConnection=true"
    )
    source_path, target_path = tmp_path / "reader.uri", tmp_path / "writer.uri"
    source_path.write_text(uri)
    target_path.hardlink_to(source_path)
    args = [
        "apply",
        "--source-uri-file",
        str(source_path),
        "--source-database",
        "source",
        "--target-uri-file",
        str(target_path),
        "--target-database",
        "target",
        "--expected-source-snapshot-sha256",
        "a" * 64,
        "--isolated-and-writers-stopped",
    ]

    def unexpected_database(*args):
        pytest.fail("Recovery connected despite unsafe credentials")

    monkeypatch.setattr(recover_mongo, "_database", unexpected_database)
    parsed = recover_mongo._parser().parse_args(args)
    with pytest.raises(MigrationError, match="distinct URI files"):
        recover_mongo._execute(parsed)
    target_path.unlink()
    target_path.write_text(uri.replace("password", "different-password"))
    with pytest.raises(MigrationError, match="distinct authenticated principals"):
        recover_mongo._execute(parsed)


def test_cli_separates_unknown_commit_and_unavailable(monkeypatch, capsys):
    receipt = {"status": "commit_outcome_unknown", "target_snapshot_sha256": "a" * 64}

    def uncertain(_args):
        raise recover_mongo.RecoveryCommitUnknown(receipt)

    monkeypatch.setattr(recover_mongo, "_execute", uncertain)
    args = ["inspect-source", "--source-uri-file", "expected", "--source-database", "source"]
    assert recover_mongo.main(args) == 3
    assert json.loads(capsys.readouterr().out) == receipt

    def unavailable(_args):
        raise OperationFailure("mongodb://synthetic-user:secret@localhost")

    monkeypatch.setattr(recover_mongo, "_execute", unavailable)
    assert recover_mongo.main(args) == 4
    captured = capsys.readouterr()
    assert "secret" not in captured.err and captured.out == ""
