"""Stdlib operation interpreter and independent expected state, shared by engines.

Only fixture setup uses trusted account/session repository writes. Neither oracle
calls production ETag/digest/ownership/duplicate helpers to calculate expectations.
"""

import copy
import hashlib
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.persistence.json import JsonPersistence
from app.services.copies import CopyService
from app.services.personal import PersonalCollectionService
from app.storage_v2 import V2Store

from .corpus import PRIVATE_PREFIX, RECEIPT_PREFIX

NOW = 1000
TTL = 86400
ROLES = ("member", "member", "admin", "manager")


class Fixture:
    def __init__(self, backend, path=None):
        self.backend, self.path = backend, path
        self.actors = []
        for index, role in enumerate(ROLES):
            identity = str(uuid.uuid4())
            token = "synthetic-property-" + uuid.uuid4().hex
            self.actors.append({"id": identity, "token": token, "role": role})
        self.movie = {
            "id": str(uuid.uuid4()),
            "title": "Propriété " + uuid.uuid4().hex,
            "year": 2026,
            "director": "Auteur",
            "actors": [],
            "genres": [],
            "synopsis": "",
        }

        def seed(work):
            for actor in self.actors:
                work.accounts.insert(
                    {
                        "id": actor["id"],
                        "username": "pbt_" + uuid.uuid4().hex[:20],
                        "display_name": "Synthétique",
                        "password_hash": "$argon2id$synthetic-property-fixture",
                        "role": actor["role"],
                        "enabled": True,
                        "created_at": 0,
                    }
                )
                work.sessions.insert(self.session(actor, NOW))
            work.catalogue.insert_movie(self.movie)

        backend.run(seed)

    @staticmethod
    def session(actor, now):
        return {
            "id": str(uuid.uuid4()),
            "user_id": actor["id"],
            "token_hash": hashlib.sha256(actor["token"].encode("ascii")).hexdigest(),
            "created_at": now,
            "last_seen": now,
            "expires_at": now + 28800,
        }

    def renew(self, now):
        # Trusted fixture renewal, not a password/login test. New sessions obey
        # the real 8h/30min bounds; no 24h session is fabricated for receipt TTL.
        def renew(work):
            for actor in self.actors:
                work.sessions.revoke_user(actor["id"])
                actor["token"] = "synthetic-property-" + uuid.uuid4().hex
                work.sessions.insert(self.session(actor, now))

        self.backend.run(renew)

    def session_state(self, actor):
        digest = hashlib.sha256(actor["token"].encode("ascii")).hexdigest()
        return self.backend.query(lambda work: work.sessions.by_token_hash(digest))


@contextmanager
def scenario_fixture(backend=None):
    if backend is not None:
        yield Fixture(backend)
    else:
        with tempfile.TemporaryDirectory(prefix="cinevault-property-") as temp:
            path = Path(temp) / "enterprise.json"
            V2Store(path).initialize()
            yield Fixture(JsonPersistence(path), path)


def assert_resource(result, expected):
    assert {key: value for key, value in result.body.items() if key != "_etag"} == expected
    assert result.body["_etag"] == result.headers["ETag"]
    return result.headers["ETag"]


def private_scenario(actions, *, backend=None):
    """Twenty final-service actions; normal refusals may touch the session."""
    with scenario_fixture(backend) as fixture:
        return run_private_actions(fixture, PRIVATE_PREFIX + list(actions))


def run_private_actions(fixture, actions):
    service = PersonalCollectionService(fixture.backend)
    model, versions, identities, expected_events = {}, {}, [], []
    for step, (operation, actor_index, selector, name) in enumerate(actions, start=1):
        now = NOW + step
        actor = fixture.actors[actor_index]
        token = actor["token"]
        identity = identities[selector % len(identities)] if identities else str(uuid.uuid4())
        target = model.get(identity)
        owned = target is not None and target["owner_id"] == actor["id"]
        if operation == 0:
            duplicate = any(
                entry["owner_id"] == actor["id"]
                and entry["list_name"].casefold() == name.casefold()
                for entry in model.values()
            )
            result = service.create(
                token,
                {"movie_id": fixture.movie["id"], "kind": "custom", "list_name": name},
                timestamp=now,
            )
            assert result.status == (409 if duplicate else 201)
            if not duplicate:
                identity = result.body["id"]
                assert identity not in identities
                expected = {
                    "id": identity,
                    "owner_id": actor["id"],
                    "movie_id": fixture.movie["id"],
                    "kind": "custom",
                    "list_name": name,
                    "supports": [],
                    "created_at": now,
                }
                model[identity] = expected
                versions[identity] = [assert_resource(result, expected)]
                identities.append(identity)
                expected_events.append((identity, actor["id"], "collection.created", now))
        elif operation in (1, 2):
            result = service.get(token, identity, timestamp=now)
            assert result.status == (200 if owned else 404), "Private ownership boundary"
            if owned:
                assert_resource(result, target)
            else:
                assert result.body is None
        elif operation in (3, 4, 5, 6):
            supplied = versions[identity][-1] if identity in versions else '"unknown"'
            if operation == 4:
                supplied = '"stale-property-version"'
            if operation == 6:
                supplied = None
            expected_status = (
                404 if not owned else 412 if operation == 4 else 428 if operation == 6 else None
            )
            duplicate = (
                operation == 3
                and owned
                and any(
                    other["id"] != identity
                    and other["owner_id"] == actor["id"]
                    and other["list_name"].casefold() == name.casefold()
                    for other in model.values()
                )
            )
            if operation in (3, 4):
                result = service.replace(
                    token,
                    identity,
                    {"list_name": name, "supports": []},
                    if_match=supplied,
                    timestamp=now,
                )
                assert result.status == (expected_status or (409 if duplicate else 200)), (
                    "ETag/precondition boundary"
                )
                if result.status == 200:
                    expected = {**target, "list_name": name}
                    version = assert_resource(result, expected)
                    if expected != target:
                        assert version != versions[identity][-1]
                    versions[identity].append(version)
                    model[identity] = expected
                    expected_events.append((identity, actor["id"], "collection.updated", now))
            else:
                result = service.delete(token, identity, if_match=supplied, timestamp=now)
                assert result.status == (expected_status or 204)
                if result.status == 204:
                    del model[identity]
                    expected_events.append((identity, actor["id"], "collection.deleted", now))
        else:
            limit, offset = 1 + selector % 3, selector % 4
            result = service.entries(
                token, [("limit", str(limit)), ("offset", str(offset))], timestamp=now
            )
            expected = sorted(
                (row for row in model.values() if row["owner_id"] == actor["id"]),
                key=lambda row: (row["created_at"], row["id"]),
                reverse=True,
            )
            assert result.status == 200 and result.body["total"] == len(expected)
            page = expected[offset : offset + limit]
            assert [
                {k: v for k, v in row.items() if k != "_etag"} for row in result.body["entries"]
            ] == page
            assert set(result.body["movies"]) == ({fixture.movie["id"]} if page else set())
        assert fixture.session_state(actor)["last_seen"] == now
    events, _ = fixture.backend.query(lambda work: work.events.page(100, 0))
    actor_ids = {actor["id"] for actor in fixture.actors}
    actual = [
        (e["entity_id"], e["actor_id"], e["action"], e["timestamp"])
        for e in events
        if e["actor_id"] in actor_ids
    ]
    assert actual == list(reversed(expected_events)), "Exactly one minimal audit per mutation"
    for event in events:
        if event["actor_id"] in actor_ids:
            assert set(event) == {
                "id",
                "actor_id",
                "entity_id",
                "entity_type",
                "action",
                "timestamp",
            }
    return len(actions)


def receipt_scenario(actions, *, backend=None):
    # Includes deletion then replay (historical reference is allowed), conflict,
    # actor-scoped reuse, and exact TTL with genuine fresh valid session fixtures.
    with scenario_fixture(backend) as fixture:
        return run_copy_actions(fixture, RECEIPT_PREFIX + list(actions))


def run_copy_actions(fixture, actions):
    service = CopyService(fixture.backend)
    receipts, copies, order = {}, {}, []
    now = NOW
    for operation, actor_slot, key_slot, location in actions:
        actor = fixture.actors[2 + actor_slot % 2]
        if operation % 4 == 2:
            now += TTL
            fixture.renew(now)
            continue
        if operation % 4 == 1:
            if order:
                identity = order[key_slot % len(order)]
                current = copies.get(identity)
                result = service.delete(
                    actor["token"],
                    identity,
                    if_match=current["_etag"] if current else '"old"',
                    timestamp=now,
                )
                assert result.status == (204 if current else 404)
                copies.pop(identity, None)
            continue
        key = None if operation % 4 == 3 else "property-key-000000000" + str(key_slot)
        data = {
            "movie_id": fixture.movie["id"],
            "support": "dvd",
            "location": location,
            "condition": "good",
        }
        if key is not None:
            receipts = {k: v for k, v in receipts.items() if v["expires_at"] > now}
        address = (actor["id"], key)
        previous = receipts.get(address) if key is not None else None
        result = service.create(actor["token"], data, key=key, timestamp=now)
        if previous is not None:
            if previous["data"] != data:
                assert result.status == 409 and result.error["code"] == "operation_conflict", (
                    "Receipt content conflict"
                )
            else:
                assert result.status == 201 and result.body == previous["body"]
                assert result.headers["Idempotency-Replayed"] == "true"
                assert result.headers["ETag"] == previous["body"]["_etag"]
        else:
            assert result.status == 201 and "Idempotency-Replayed" not in result.headers, (
                "New operation must not be a replay"
            )
            identity = result.body["id"]
            assert identity not in order, "Expired key must create a new identity"
            assert_resource(result, {"id": identity, **data})
            copies[identity] = copy.deepcopy(result.body)
            order.append(identity)
            if key is not None:
                receipts[address] = {
                    "data": data,
                    "body": copy.deepcopy(result.body),
                    "expires_at": now + TTL,
                }
        actual = fixture.backend.query(
            lambda work: work.catalogue.copies_for_movie(fixture.movie["id"])
        )
        assert {row["id"]: row for row in actual} == {
            identity: {k: v for k, v in row.items() if k != "_etag"}
            for identity, row in copies.items()
        }, "Replay must not duplicate or resurrect a deleted copy"
        # Count only our actors: the native database may contain earlier contracts.
        for address, expected in receipts.items():
            actor_id, raw_key = address
            hashed = hashlib.sha256(raw_key.encode("ascii")).hexdigest()
            stored = fixture.backend.query(lambda work: work.receipts.get(actor_id, hashed))
            assert stored["response"] == expected["body"]
    return len(actions)
