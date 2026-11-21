"""Recover a verified MongoDB v2 snapshot into a separately provisioned database."""

import json
import sys
from contextlib import ExitStack
from pathlib import Path

from argon2.exceptions import HashingError
from bson.errors import BSONError
from pymongo import MongoClient, ReadPreference
from pymongo.errors import ConfigurationError, InvalidURI, PyMongoError
from pymongo.uri_parser import parse_uri

from .account_cli import OperatorArgumentParser, require_interactive
from .migrate_backends import (
    MigrationError,
    _require_sha256,
    empty_native_snapshot,
    prepare_native_snapshot,
    publish_native_snapshot,
    read_verified_snapshot,
    require_database_name,
)
from .persistence.errors import CommitOutcomeUnknown, TransactionUnavailable
from .persistence.factory import _connection_secret
from .persistence.mongo_model import INT64_MAX
from .recovery import quarantine_accounts, read_password


class RecoveryCommitUnknown(CommitOutcomeUnknown):
    """The candidate transaction may have committed; carry its opaque receipt."""

    def __init__(self, receipt):
        super().__init__("MongoDB recovery commit outcome is unknown")
        self.receipt = receipt


def inspect_source(database):
    """Identify the validated native source without exposing its contents."""
    _, receipt = read_verified_snapshot(database)
    return {"status": "inspected", **receipt}


def _require_distinct_databases(source, target):
    require_database_name(source)
    require_database_name(target)
    if source == target:
        raise MigrationError("MongoDB recovery requires distinct source and target database names")


def apply(
    source,
    target,
    username,
    display_name,
    password,
    *,
    expected_source_snapshot_sha256,
    timestamp=None,
):
    """Prepare a quarantined copy in memory and publish it using one transaction."""
    _require_distinct_databases(source.name, target.name)
    document, source_receipt = read_verified_snapshot(source)
    _require_sha256(
        expected_source_snapshot_sha256,
        source_receipt["snapshot_sha256"],
        "MongoDB source changed since inspection",
    )
    if document["revision"] == INT64_MAX:
        raise MigrationError("MongoDB source revision cannot be incremented")
    _, initial_target = read_verified_snapshot(target)
    if not empty_native_snapshot(initial_target["revision"], initial_target["counts"]):
        raise MigrationError("MongoDB recovery requires an empty provisioned target")
    try:
        report = quarantine_accounts(
            document, username, display_name, password, timestamp=timestamp
        )
    except ValueError as exc:
        raise MigrationError(str(exc)) from None
    snapshot, native_receipt = prepare_native_snapshot(document)
    receipt = {
        **report,
        "source_snapshot_sha256": source_receipt["snapshot_sha256"],
        "target_snapshot_sha256": native_receipt["snapshot_sha256"],
        "revision": native_receipt["revision"],
    }
    # All retries inside the publisher use this exact candidate and operation ID.
    try:
        status = publish_native_snapshot(target, snapshot, native_receipt)
    except (CommitOutcomeUnknown, KeyboardInterrupt):
        raise RecoveryCommitUnknown({"status": "commit_outcome_unknown", **receipt}) from None
    return {"status": status, **receipt}


def reconcile(target, *, expected_target_snapshot_sha256):
    """Confirm only an exact candidate, including its audit and revision."""
    _, receipt = read_verified_snapshot(target)
    try:
        _require_sha256(
            expected_target_snapshot_sha256,
            receipt["snapshot_sha256"],
            "MongoDB target does not match the intended recovery",
        )
    except MigrationError:
        raise CommitOutcomeUnknown(
            "Read-only reconciliation did not confirm the candidate"
        ) from None
    return {"status": "matched", **receipt}


class _Parser(OperatorArgumentParser):
    def error(self, message):
        if message.startswith("argument command: invalid choice:"):
            message = "Unrecognized recovery command. Use --help."
        super().error(message)


def _parser():
    parser = _Parser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect-source", "apply", "reconcile"):
        command = commands.add_parser(name, allow_abbrev=False)
        if name != "reconcile":
            command.add_argument("--source-uri-file", required=True, type=Path)
            command.add_argument("--source-database", required=True)
        if name != "inspect-source":
            command.add_argument("--target-uri-file", required=True, type=Path)
            command.add_argument("--target-database", required=True)
        if name == "apply":
            command.add_argument("--expected-source-snapshot-sha256", required=True)
            command.add_argument(
                "--isolated-and-writers-stopped", action="store_true", required=True
            )
        if name == "reconcile":
            command.add_argument("--expected-target-snapshot-sha256", required=True)
    return parser


def _uri(path):
    try:
        return _connection_secret(path, parse_uri)
    except (ConfigurationError, InvalidURI, ValueError, TypeError):
        raise MigrationError("MongoDB connection configuration is invalid") from None


def _principal(uri):
    parts = parse_uri(uri, validate=True)
    return parts["options"]["authSource"], parts["username"]


def _database(stack, name, uri):
    client = stack.enter_context(
        MongoClient(
            uri,
            connect=False,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=5000,
            appname="CineVaultRecovery",
        )
    )
    return client.get_database(name, read_preference=ReadPreference.PRIMARY)


def _prompt(label):
    print(label, end="", file=sys.stderr, flush=True)
    return input()


def _execute(args):
    with ExitStack() as stack:
        if args.command == "inspect-source":
            require_database_name(args.source_database)
            source = _database(stack, args.source_database, _uri(args.source_uri_file))
            return inspect_source(source)
        if args.command == "reconcile":
            require_database_name(args.target_database)
            target = _database(stack, args.target_database, _uri(args.target_uri_file))
            return reconcile(
                target, expected_target_snapshot_sha256=args.expected_target_snapshot_sha256
            )

        _require_distinct_databases(args.source_database, args.target_database)
        if args.source_uri_file.samefile(args.target_uri_file):
            raise MigrationError("MongoDB recovery requires distinct URI files")
        source_uri, target_uri = _uri(args.source_uri_file), _uri(args.target_uri_file)
        if _principal(source_uri) == _principal(target_uri):
            raise MigrationError("MongoDB recovery requires distinct authenticated principals")
        return apply(
            _database(stack, args.source_database, source_uri),
            _database(stack, args.target_database, target_uri),
            _prompt("New recovery administrator username: "),
            _prompt("Display name: "),
            read_password(),
            expected_source_snapshot_sha256=args.expected_source_snapshot_sha256,
        )


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "apply":
        require_interactive(parser)
    try:
        receipt = _execute(args)
    except RecoveryCommitUnknown as exc:
        print(json.dumps(exc.receipt, sort_keys=True))
        print(
            "commit_outcome_unknown: verify the intended candidate via reconcile", file=sys.stderr
        )
        return 3
    except CommitOutcomeUnknown:
        print(
            "recovery_unconfirmed: read-only inspection did not confirm a snapshot", file=sys.stderr
        )
        return 3
    except (TransactionUnavailable, OSError, PyMongoError, HashingError):
        print(
            "recovery_unavailable: database, file or terminal access unavailable", file=sys.stderr
        )
        return 4
    except (EOFError, KeyboardInterrupt):
        print("recovery_cancelled", file=sys.stderr)
        return 1
    except MigrationError as exc:
        print(f"recovery_refused: {exc}", file=sys.stderr)
        return 2
    except (BSONError, ValueError, TypeError, KeyError, RuntimeError):
        print("recovery_refused: invalid input or candidate state", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
