"""Offline, no-clobber recovery with account quarantine and fresh credentials."""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from pathlib import Path

import portalocker

from .account_cli import (
    OperatorArgumentParser,
    add_store_arguments,
    hidden_input,
    open_operator_store,
    require_interactive,
    validate_store_arguments,
)
from .audit import record_event
from .auth_storage import ACCOUNT_CAPACITY
from .enterprise_auth import hasher, make_user
from .persistence.errors import CommitOutcomeUnknown
from .persistence.json import JsonPersistence
from .recovery_clock import activity_after
from .services.accounts import OperatorAccountService
from .services.authentication import normalize_username, valid_display, valid_password
from .storage_v2 import V2Store, _encode, _write_new, decode_document, validate_document


def check_clock(document, timestamp):
    if (
        type(timestamp) is not int
        or timestamp < 0
        or activity_after(document.__getitem__, timestamp)
    ):
        raise ValueError("Recovery clock precedes stored events")


def verified_snapshot(path, expected_sha256):
    if not isinstance(expected_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise ValueError("Invalid expected digest")
    data = Path(path).read_bytes()
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):
        raise ValueError("Snapshot digest mismatch")
    return decode_document(data)


def recover(snapshot, target, expected_sha256, username, display_name, password, *, timestamp=None):
    target = Path(target)
    if target.exists():
        raise FileExistsError("Recovery target already exists")
    document = verified_snapshot(snapshot, expected_sha256)
    timestamp = int(time.time()) if timestamp is None else timestamp
    check_clock(document, timestamp)
    username = normalize_username(username)
    if (
        not username
        or not valid_display(display_name)
        or not valid_password(password)
        or any(u["username"] == username for u in document["users"])
    ):
        raise ValueError("Invalid recovery administrator")
    if len(document["users"]) >= ACCOUNT_CAPACITY:
        raise ValueError("Account storage limit exceeded")
    admin = make_user(username, display_name, password, "admin", timestamp)
    report = {
        "operation_id": str(uuid.uuid4()),
        "source_sha256": expected_sha256,
        "target_sha256": "",
        "disabled_users": len(document["users"]),
        "revoked_sessions": len(document["sessions"]),
        "closed_reservations": 0,
    }
    for user in document["users"]:
        user["enabled"] = False
        # Discard each independent random password; re-enabling cannot revive an archived password.
        user["password_hash"] = hasher.hash(secrets.token_urlsafe(48))
    document["users"].append(admin)
    document["sessions"] = []
    document["auth_attempts"] = []
    for reservation in document["reservations"]:
        if reservation["status"] != "waiting":
            continue
        expired = reservation["expires_at"] <= timestamp
        reservation.update(status="expired" if expired else "cancelled", closed_at=timestamp)
        record_event(
            document,
            None if expired else admin["id"],
            "reservation",
            reservation["id"],
            "expired" if expired else "cancelled",
            timestamp=timestamp,
        )
        report["closed_reservations"] += 1
    record_event(
        document, admin["id"], "recovery", report["operation_id"], "completed", timestamp=timestamp
    )
    document["revision"] += 1
    validate_document(document)
    encoded = _encode(document)
    with portalocker.Lock(str(target) + ".lock", mode="a", timeout=10):
        _write_new(target, encoded)
    report["target_sha256"] = hashlib.sha256(encoded).hexdigest()
    return report


def reset_password(store, operator, username, password, *, enable=False, timestamp=None):
    if isinstance(store, V2Store):
        store = JsonPersistence(store)
    OperatorAccountService(store).reset_password(
        operator,
        username,
        password,
        enable=enable,
        timestamp=int(time.time()) if timestamp is None else timestamp,
        prepare_hash=hasher.hash,
    )


def read_password():
    password = hidden_input("New password: ")
    if password != hidden_input("Confirm new password: ") or not valid_password(password):
        raise ValueError("Password policy or confirmation failed")
    return password


def main():
    parser = OperatorArgumentParser(
        description="Offline recovery; keep API stopped and preserve the old volume."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--snapshot", required=True)
    restore.add_argument("--target", required=True)
    restore.add_argument("--sha256", required=True)
    reset = commands.add_parser("reset-password")
    add_store_arguments(reset)
    reset.add_argument("--operator", required=True)
    reset.add_argument("--username", required=True)
    reset.add_argument("--enable", action="store_true")
    args = parser.parse_args()
    if args.command == "reset-password":
        validate_store_arguments(reset, args)
    require_interactive(parser)
    try:
        if args.command == "restore":
            if Path(args.target).exists():
                raise FileExistsError()
            verified_snapshot(args.snapshot, args.sha256)
            username = input("New recovery administrator username: ")
            display_name = input("Display name: ")
            report = recover(
                args.snapshot, args.target, args.sha256, username, display_name, read_password()
            )
            print(json.dumps(report, sort_keys=True))
        else:
            password = read_password()
            with open_operator_store(args) as persistence:
                reset_password(
                    persistence,
                    args.operator,
                    args.username,
                    password,
                    enable=args.enable,
                )
            print(
                "Password reset; existing sessions revoked. Account enabled only if explicitly requested."
            )
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "Recovery operation cancelled.\n")
    except CommitOutcomeUnknown:
        parser.exit(1, "Password reset outcome is uncertain. Verify before retrying.\n")
    except (ValueError, OSError, RuntimeError, portalocker.exceptions.LockException):
        parser.exit(
            1, "Recovery operation failed. Inspect the storage and clock before retrying.\n"
        )


if __name__ == "__main__":
    main()
