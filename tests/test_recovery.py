import hashlib
import subprocess
import sys

import pytest
from test_enterprise_auth import account
from test_enterprise_auth import enterprise as enterprise
from test_enterprise_catalogue import COPY, MOVIE

from app import create_app
from app.storage_v2 import V2Store


def snapshot(enterprise, tmp_path):
    client, path = enterprise
    _, manager = account(client, "restoremanager", "manager", path)
    _, borrower = account(client, "restoreborrower")
    _, waiting = account(client, "restorewaiting")
    movie = client.post("/api/v1/movies", headers=manager, json=MOVIE).json
    copy = client.post(
        "/api/v1/copies",
        headers={**manager, "Idempotency-Key": "synthetic-recovery-copy-key-1234567"},
        json={"movie_id": movie["id"], **COPY},
    ).json
    assert (
        client.post(
            "/api/v1/collections",
            headers=borrower,
            json={"movie_id": movie["id"], "kind": "favorite"},
        ).status_code
        == 201
    )
    loan = client.post("/api/v1/loans", headers=borrower, json={"movie_id": movie["id"]}).json
    import time

    active = client.post(
        "/api/v1/loans/" + loan["id"] + "/approve",
        headers={**manager, "If-Match": loan["_etag"]},
        json={"copy_id": copy["id"], "due_at": int(time.time()) + 3600},
    )
    assert active.status_code == 200
    assert (
        client.post(
            "/api/v1/reservations", headers=waiting, json={"movie_id": movie["id"]}
        ).status_code
        == 201
    )
    archive = tmp_path / "backup.json"
    digest = V2Store(path).snapshot(archive)
    return client, path, archive, digest, borrower


def app_for(path):
    return create_app(
        {
            "TESTING": True,
            "DATA_FILE": str(path.parent / "legacy.json"),
            "ENTERPRISE_ENABLED": True,
            "ENTERPRISE_DATA_FILE": str(path),
            "AUTH_THROTTLE_KEY": b"x" * 32,
        }
    ).test_client()


def test_operational_restore_preserves_business_but_revokes_historical_access(enterprise, tmp_path):
    from app.recovery import recover

    client, path, archive, digest, old_bearer = snapshot(enterprise, tmp_path)
    original = path.read_bytes()
    backup = archive.read_bytes()
    before = V2Store(path).read()
    target = tmp_path / "restored.json"
    report = recover(
        archive, target, digest, "recoveryadmin", "Recovery admin", "fresh test passphrase 2026"
    )
    assert set(report) == {
        "operation_id",
        "source_sha256",
        "target_sha256",
        "disabled_users",
        "revoked_sessions",
        "closed_reservations",
    }
    assert (
        report["disabled_users"] == 3
        and report["revoked_sessions"] == 3
        and report["closed_reservations"] == 1
    )
    assert hashlib.sha256(target.read_bytes()).hexdigest() == report["target_sha256"]
    assert path.read_bytes() == original and archive.read_bytes() == backup
    restored = V2Store(target).read()
    for key in ("movies", "copies", "collections", "loans", "idempotency_records"):
        assert restored[key] == before[key]
    assert restored["sessions"] == restored["auth_attempts"] == []
    assert restored["reservations"][0]["status"] == "cancelled"
    assert [u["username"] for u in restored["users"] if u["enabled"]] == ["recoveryadmin"]
    assert restored["audit_events"][-1]["action"] == "recovery.completed"
    assert b"fresh test passphrase" not in target.read_bytes()
    api = app_for(target)
    assert api.get("/api/v1/me", headers=old_bearer).status_code == 401
    assert (
        api.post(
            "/api/v1/auth/login",
            json={"username": "recoveryadmin", "password": "fresh test passphrase 2026"},
        ).status_code
        == 200
    )
    # Even accidental re-enabling cannot revive the password from the archive.
    V2Store(target).transact(
        lambda doc: next(u for u in doc["users"] if u["username"] == "restoreborrower").update(
            enabled=True
        )
    )
    assert (
        api.post(
            "/api/v1/auth/login",
            json={"username": "restoreborrower", "password": "test passphrase 123"},
        ).status_code
        == 401
    )


def test_recovery_never_clobbers_and_rejects_bad_archive_or_clock(enterprise, tmp_path):
    from app.recovery import recover

    client, path, archive, digest, bearer = snapshot(enterprise, tmp_path)
    target = tmp_path / "new.json"
    args = (
        archive,
        target,
        digest,
        "recoveryadmin",
        "Recovery admin",
        "fresh test passphrase 2026",
    )
    with pytest.raises(ValueError):
        recover(archive, target, "0" * 64, *args[3:])
    with pytest.raises(ValueError):
        recover(*args, timestamp=1)
    with pytest.raises(ValueError):
        recover(
            archive,
            target,
            digest,
            "restoremanager",
            "Recovery admin",
            "fresh test passphrase 2026",
        )
    assert not target.exists()
    target.write_bytes(b"preserve me")
    with pytest.raises(FileExistsError):
        recover(*args)
    assert target.read_bytes() == b"preserve me"


def test_reset_of_verified_account_requires_active_admin_and_does_not_change_role(
    enterprise, tmp_path
):
    from app.recovery import recover, reset_password

    client, path, archive, digest, bearer = snapshot(enterprise, tmp_path)
    target = tmp_path / "restored.json"
    recover(
        archive, target, digest, "recoveryadmin", "Recovery admin", "fresh test passphrase 2026"
    )
    store = V2Store(target)
    with pytest.raises(ValueError):
        reset_password(
            store, "restoremanager", "restoreborrower", "replacement passphrase 2026", enable=True
        )
    reset_password(store, "recoveryadmin", "restoreborrower", "replacement passphrase 2026")
    user = next(u for u in store.read()["users"] if u["username"] == "restoreborrower")
    assert user["role"] == "member" and user["enabled"] is False
    reset_password(
        store, "recoveryadmin", "restoreborrower", "another replacement 2026", enable=True
    )
    api = app_for(target)
    assert (
        api.post(
            "/api/v1/auth/login",
            json={"username": "restoreborrower", "password": "another replacement 2026"},
        ).status_code
        == 200
    )
    assert (
        api.post(
            "/api/v1/auth/login",
            json={"username": "restoreborrower", "password": "test passphrase 123"},
        ).status_code
        == 401
    )
    assert store.read()["audit_events"][-1]["action"] == "recovery.password_reset"


def test_recovery_cli_refuses_piped_passwords(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.recovery",
            "restore",
            "--snapshot",
            str(tmp_path / "missing.json"),
            "--target",
            str(tmp_path / "new.json"),
            "--sha256",
            "0" * 64,
        ],
        input="must-not-be-read\n",
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0 and "interactive terminal" in result.stderr
    assert "must-not-be-read" not in result.stdout + result.stderr


def recover_in_process(archive, target, digest, queue):
    from app.recovery import recover

    try:
        recover(
            archive, target, digest, "recoveryadmin", "Recovery admin", "fresh test passphrase 2026"
        )
        queue.put("created")
    except FileExistsError:
        queue.put("exists")


def test_two_processes_restore_one_valid_target_without_clobber(enterprise, tmp_path):
    import json
    import multiprocessing
    from pathlib import Path

    from jsonschema import Draft202012Validator

    client, path, archive, digest, bearer = snapshot(enterprise, tmp_path)
    target = tmp_path / "recovered.json"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=recover_in_process, args=(archive, target, digest, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join()
            raise AssertionError("Recovery process timed out")
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=5) for _ in processes) == ["created", "exists"]
    doc = V2Store(target).read()
    assert len([e for e in doc["audit_events"] if e["action"] == "recovery.completed"]) == 1
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/catalogue-v2.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(doc)


def test_recovery_write_failures_preserve_archive_and_ambiguous_commit(
    enterprise, tmp_path, monkeypatch
):
    from app.recovery import recover

    client, path, archive, digest, bearer = snapshot(enterprise, tmp_path)
    original = archive.read_bytes()
    target = tmp_path / "recovered.json"

    def fail(*args):
        raise OSError("simulated durability failure")

    with monkeypatch.context() as patch:
        patch.setattr("app.storage_v2.os.link", fail)
        with pytest.raises(OSError):
            recover(
                archive,
                target,
                digest,
                "recoveryadmin",
                "Recovery admin",
                "fresh test passphrase 2026",
            )
    assert not target.exists() and archive.read_bytes() == original
    assert not list(tmp_path.glob(".cinevault-v2-*"))
    with monkeypatch.context() as patch:
        patch.setattr("app.storage_v2._sync_directory", fail)
        with pytest.raises(OSError):
            recover(
                archive,
                target,
                digest,
                "recoveryadmin",
                "Recovery admin",
                "fresh test passphrase 2026",
            )
    # A failure after the atomic link may leave a valid target; preserve and inspect it.
    assert target.exists() and V2Store(target).read()["sessions"] == []
    assert archive.read_bytes() == original
    with pytest.raises(FileExistsError):
        recover(
            archive, target, digest, "recoveryadmin", "Recovery admin", "fresh test passphrase 2026"
        )


def test_invalid_schema_with_matching_digest_is_not_restored(tmp_path):
    from app.recovery import recover

    archive = tmp_path / "bad.json"
    archive.write_bytes(b'{"schema_version":999}')
    target = tmp_path / "new.json"
    with pytest.raises(ValueError):
        recover(
            archive,
            target,
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            "recoveryadmin",
            "Recovery admin",
            "fresh test passphrase 2026",
        )
    assert not target.exists()


def test_past_due_waiting_reservation_expires_at_controlled_restore_time(enterprise, tmp_path):
    from app.recovery import recover

    client, path, archive, digest, bearer = snapshot(enterprise, tmp_path)
    timestamp = V2Store(path).read()["reservations"][0]["expires_at"]
    target = tmp_path / "expired-recovery.json"
    recover(
        archive,
        target,
        digest,
        "recoveryadmin",
        "Recovery admin",
        "fresh test passphrase 2026",
        timestamp=timestamp,
    )
    doc = V2Store(target).read()
    assert doc["reservations"][0]["status"] == "expired"
    assert doc["reservations"][0]["closed_at"] == timestamp
    assert any(
        e["action"] == "reservation.expired" and e["actor_id"] is None for e in doc["audit_events"]
    )
