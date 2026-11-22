"""Targeted MongoDB repositories bound to one owned transaction/session."""

import re
from functools import cache
from itertools import batched, islice
from os import getpid
from threading import get_ident

from bson import Int64
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure

from ..auth_storage import SESSION_CAPACITY, USERNAME
from ..business_storage import BUSINESS_CAPACITIES
from ..catalogue_query import MovieQuery
from ..personal_model import AUDIT_KEEP, PersonalQuery
from ..storage_v2 import _valid_uuid, empty_document, validate_document
from .contracts import Record
from .mongo_model import INT64_MAX, collection_name, decode_record, encode_record

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TRANSFER_BATCH = 500
_CAPACITIES = {"sessions": SESSION_CAPACITY, **BUSINESS_CAPACITIES}


def _identity(value):
    if not _valid_uuid(value):
        raise ValueError("Identity must be a canonical UUID string")


def _timestamp(value):
    if type(value) is not int or not 0 <= value <= INT64_MAX:
        raise ValueError("Timestamp must be a non-negative BSON Int64 integer")


class _MongoScope:
    def __init__(self, database, session, revision, *, writable):
        self._database = database
        self._session = session
        self._writable = writable
        self._revision = revision
        self._owner = (getpid(), get_ident())
        self._failure = None
        self._capacity_checks = set()
        self._dirty = {
            name: set()
            for name in (
                "users",
                "sessions",
                "movies",
                "copies",
                "auth_attempts",
                "idempotency_records",
                "collections",
                "audit_events",
                "loans",
                "reservations",
            )
        }
        self._deleted = {name: set() for name in ("movies", "copies")}
        self._imported = {name: set() for name in ("movies", "copies")}

    def check(self, *, write=False):
        if self._session is None:
            raise RuntimeError("Unit of work is closed")
        if self._owner != (getpid(), get_ident()):
            raise RuntimeError("Unit of work must stay with its owner")
        if self._failure is not None:
            raise self._failure
        if write and not self._writable:
            raise RuntimeError("Query unit of work is read-only")

    def collection(self, name):
        self.check()
        return self._database[collection_name(name)]

    def find(self, name, selector, projection=None):
        return self.collection(name).find_one(selector, projection, session=self._session)

    def rows_by_ids(self, collection, identities, projection=None):
        self.check()
        rows = []
        for group in batched(identities, _TRANSFER_BATCH):
            with collection.find(
                {"_id": {"$in": list(group)}}, projection, session=self._session
            ) as cursor:
                rows.extend(cursor)
        return rows

    def write(self, method, *args, **kwargs):
        self.check(write=True)
        try:
            return method(*args, session=self._session, **kwargs)
        except (DuplicateKeyError, OperationFailure) as exc:
            bulk_constraint = isinstance(exc, BulkWriteError) and any(
                error.get("code") in (11000, 121)
                for error in (exc.details or {}).get("writeErrors", ())
            )
            if (
                isinstance(exc, DuplicateKeyError) or exc.code == 121 or bulk_constraint
            ) and not exc.has_error_label("TransientTransactionError"):
                self._failure = ValueError("MongoDB entity violates a persistence constraint")
                raise self._failure from None
            raise

    def claim(self, identity, name):
        self.write(self._database.entity_ids.insert_one, {"_id": identity, "kind": name})

    def release(self, identities, name):
        result = self.write(
            self._database.entity_ids.delete_many,
            {"_id": {"$in": identities}, "kind": name},
        )
        if result.deleted_count != len(identities):
            self._failure = ValueError("Entity identity registry is inconsistent")
            raise self._failure

    def registered(self, identity, name):
        self.check()
        if (
            self._database.entity_ids.find_one(
                {"_id": identity, "kind": name}, {"_id": 1}, session=self._session
            )
            is None
        ):
            raise ValueError("Entity identity registry is inconsistent")

    @staticmethod
    def _parent(document, name, identity, find_reference):
        if identity is None:
            return
        raw = find_reference(name, identity)
        if raw is not None and all(row["id"] != identity for row in document[name]):
            document[name].append(decode_record(name, raw))

    def _loan_parents(self, document, loan, find_reference):
        self._parent(document, "users", loan["borrower_id"], find_reference)
        self._parent(document, "movies", loan["movie_id"], find_reference)
        self._parent(document, "copies", loan["copy_id"], find_reference)

    def _circulation_fragment(self, document, name, record, find_reference, checked_waiting):
        if name == "loans":
            self._loan_parents(document, record, find_reference)
            linked = self.find("reservations", {"loan_id": record["id"], "status": "fulfilled"})
            if linked is not None:
                linked = decode_record("reservations", linked)
                document["reservations"].append(linked)
                self._parent(document, "users", linked["user_id"], find_reference)
                self._parent(document, "movies", linked["movie_id"], find_reference)
            for status, limit in (("pending", 10), ("active", 5)):
                count = self.collection("loans").count_documents(
                    {"borrower_id": record["borrower_id"], "status": status},
                    session=self._session,
                )
                if count > limit:
                    raise ValueError("Borrower loan limit exceeded")
        else:
            user_id = record["user_id"]
            self._parent(document, "users", user_id, find_reference)
            self._parent(document, "movies", record["movie_id"], find_reference)
            if record["loan_id"] is not None:
                self._parent(document, "loans", record["loan_id"], find_reference)
                for loan in document["loans"]:
                    self._loan_parents(document, loan, find_reference)
            if user_id not in checked_waiting:
                count = self.collection("reservations").count_documents(
                    {"user_id": user_id, "status": "waiting"}, session=self._session
                )
                if count > 10:
                    raise ValueError("Reservation limit exceeded")
                checked_waiting.add(user_id)

    def _validate_imported_catalogue(self):
        if not any(self._imported.values()):
            return
        document = empty_document()
        document["revision"] = self._revision
        for name, identities in self._imported.items():
            rows = self.rows_by_ids(self.collection(name), sorted(identities))
            document[name] = [decode_record(name, row) for row in rows]
        known_movies = {movie["id"] for movie in document["movies"]}
        parent_ids = {copy["movie_id"] for copy in document["copies"]} - known_movies
        parents = self.rows_by_ids(self.collection("movies"), sorted(parent_ids))
        document["movies"].extend(decode_record("movies", row) for row in parents)
        for group in batched(document["copies"], _TRANSFER_BATCH):
            mismatches = [
                {"copy_id": item["id"], "movie_id": {"$ne": item["movie_id"]}} for item in group
            ]
            if self.find("loans", {"$or": mismatches}, {"_id": 1}) is not None:
                raise ValueError("Copy history cannot change its movie reference")
        validate_document(document)

    def validate(self):
        """Validate touched entities and their final references, never a DB snapshot."""
        self.check()
        # Count only collections that gained an entity, after all changes in this transaction.
        for name in sorted(self._capacity_checks):
            limit = _CAPACITIES[name]
            count = self.collection(name).count_documents(
                {}, hint="_id_", limit=limit + 1, session=self._session
            )
            if count > limit:
                raise ValueError("Storage collection limit exceeded")
        self._validate_imported_catalogue()

        # Validation runs after the command's writes against one transaction snapshot.
        @cache
        def find_reference(name, identity):
            return self.find(name, {"_id": identity})

        checked_waiting = set()
        for name, identities in self._dirty.items():
            for identity in identities:
                if identity in self._imported.get(name, ()):
                    continue  # The batch check just validated its final transactional state.
                raw = self.find(name, {"_id": identity})
                if raw is None:
                    continue
                record = decode_record(name, raw)
                document = empty_document()
                document["revision"] = self._revision
                document[name] = [record]
                if name in ("loans", "reservations"):
                    self._circulation_fragment(
                        document, name, record, find_reference, checked_waiting
                    )
                elif name == "collections":
                    parent = self.find("users", {"_id": record["owner_id"]})
                    movie = self.find("movies", {"_id": record["movie_id"]})
                    if parent is not None:
                        document["users"] = [decode_record("users", parent)]
                    if movie is not None:
                        document["movies"] = [decode_record("movies", movie)]
                elif name == "audit_events":
                    if record["actor_id"] is not None:
                        parent = self.find("users", {"_id": record["actor_id"]})
                        if parent is not None:
                            document["users"] = [decode_record("users", parent)]
                elif name in ("sessions", "idempotency_records"):
                    parent_id = record["user_id"] if name == "sessions" else record["actor_id"]
                    parent = self.find("users", {"_id": parent_id})
                    if parent is not None:
                        document["users"] = [decode_record("users", parent)]
                elif name == "copies":
                    parent = self.find("movies", {"_id": record["movie_id"]})
                    if parent is not None:
                        document["movies"] = [decode_record("movies", parent)]
                    if (
                        self.find(
                            "loans", {"copy_id": identity, "movie_id": {"$ne": record["movie_id"]}}
                        )
                        is not None
                    ):
                        raise ValueError("Copy history cannot change its movie reference")
                validate_document(document)
                if name == "users" and not record["enabled"]:
                    if self.find("reservations", {"user_id": identity, "status": "waiting"}):
                        raise ValueError("Disabled account still has waiting reservations")
        for identity in self._deleted["movies"]:
            if self.find("movies", {"_id": identity}) is None and any(
                self.find(name, {"movie_id": identity}, {"_id": 1}) is not None
                for name in ("copies", "collections", "loans", "reservations")
            ):
                raise ValueError("Movie still has business or historical references")
        for identity in self._deleted["copies"]:
            if (
                self.find("copies", {"_id": identity}) is None
                and self.find("loans", {"copy_id": identity}, {"_id": 1}) is not None
            ):
                raise ValueError("Copy still has historical loan references")

    def close(self):
        self._session = None
        self._database = None


class _Repository:
    def __init__(self, scope):
        self._scope = scope

    def _get(self, name, identity):
        self._scope.check()
        _identity(identity)
        record = self._scope.find(name, {"_id": identity})
        return decode_record(name, record) if record is not None else None

    def _insert(self, name, record):
        self._scope.check(write=True)
        encoded = encode_record(name, record)
        self._scope.claim(encoded["_id"], name)
        self._scope.write(self._scope.collection(name).insert_one, encoded)
        self._scope._dirty[name].add(encoded["_id"])
        if name in _CAPACITIES:
            self._scope._capacity_checks.add(name)

    def _save(self, name, record):
        self._scope.check(write=True)
        encoded = encode_record(name, record)
        identity = encoded["_id"]
        if self._scope.find(name, {"_id": identity}, {"_id": 1}) is None:
            raise LookupError("Persistence identity does not exist")
        self._scope.registered(identity, name)
        result = self._scope.write(
            self._scope.collection(name).replace_one, {"_id": identity}, encoded, upsert=False
        )
        if result.matched_count != 1:
            raise LookupError("Persistence identity does not exist")
        self._scope._dirty[name].add(identity)

    def _delete(self, name, identity):
        self._scope.check(write=True)
        _identity(identity)
        if self._scope.find(name, {"_id": identity}, {"_id": 1}) is not None:
            self._scope.registered(identity, name)
        result = self._scope.write(self._scope.collection(name).delete_one, {"_id": identity})
        if result.deleted_count:
            self._scope.release([identity], name)
            if name in self._scope._deleted:
                self._scope._deleted[name].add(identity)
        return bool(result.deleted_count)


class MongoAccounts(_Repository):
    def count(self) -> int:
        self._scope.check()
        return self._scope.collection("users").count_documents({}, session=self._scope._session)

    def by_ids(self, identities: list[str]) -> list[Record]:
        self._scope.check()
        if type(identities) is not list or len(identities) > 100:
            raise ValueError("Account page identities contain at most 100 items")
        for identity in identities:
            _identity(identity)
        if not identities:
            return []
        with self._scope.collection("users").find(
            {"_id": {"$in": identities}},
            session=self._scope._session,
        ) as cursor:
            return [decode_record("users", row) for row in cursor]

    def get(self, identity: str) -> Record | None:
        return self._get("users", identity)

    def by_username(self, username: str) -> Record | None:
        self._scope.check()
        if not isinstance(username, str) or USERNAME.fullmatch(username) is None:
            raise ValueError("Username must be canonical")
        record = self._scope.find("users", {"username": username})
        return decode_record("users", record) if record is not None else None

    def page(self, limit: int, offset: int) -> list[Record]:
        self._scope.check()
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise ValueError("Invalid account page")
        cursor = self._scope.collection("users").find({}, session=self._scope._session)
        with cursor.sort("username", 1).skip(offset).limit(limit):
            return [decode_record("users", record) for record in cursor]

    def has_admin(self) -> bool:
        return self._scope.find("users", {"role": "admin"}, {"_id": 1}) is not None

    def has_other_enabled_admin(self, identity: str) -> bool:
        self._scope.check()
        _identity(identity)
        return (
            self._scope.find(
                "users", {"_id": {"$ne": identity}, "role": "admin", "enabled": True}, {"_id": 1}
            )
            is not None
        )

    def insert(self, account: Record) -> None:
        self._insert("users", account)

    def save(self, account: Record) -> None:
        self._save("users", account)


class MongoSessions(_Repository):
    def by_token_hash(self, token_hash: str) -> Record | None:
        self._scope.check()
        if not isinstance(token_hash, str) or _HASH.fullmatch(token_hash) is None:
            raise ValueError("Token hash must be canonical")
        record = self._scope.find("sessions", {"token_hash": token_hash})
        return decode_record("sessions", record) if record is not None else None

    def touch(self, identity: str, timestamp: int) -> None:
        self._scope.check(write=True)
        _timestamp(timestamp)
        record = self._get("sessions", identity)
        if record is None:
            raise LookupError("Persistence identity does not exist")
        record["last_seen"] = timestamp
        self._save("sessions", record)

    def insert(self, session: Record) -> None:
        self._insert("sessions", session)

    def _delete_matching(self, selector):
        self._scope.check(write=True)
        removed = 0
        cursor = self._scope.collection("sessions").find(
            selector, {"_id": 1}, session=self._scope._session
        )
        with cursor:
            while batch := list(islice(cursor, 500)):
                identities = [record["_id"] for record in batch]
                result = self._scope.write(
                    self._scope.collection("sessions").delete_many, {"_id": {"$in": identities}}
                )
                self._scope.release(identities, "sessions")
                removed += result.deleted_count
        return removed

    def revoke_user(self, user_id: str) -> int:
        self._scope.check(write=True)
        _identity(user_id)
        return self._delete_matching({"user_id": user_id})

    def revoke(self, identity: str) -> bool:
        return self._delete("sessions", identity)

    def delete_expired(self, timestamp: int, *, idle_ttl: int) -> int:
        self._scope.check(write=True)
        _timestamp(timestamp)
        if type(idle_ttl) is not int or idle_ttl <= 0:
            raise ValueError("Idle lifetime must be a positive integer")
        conditions = [{"expires_at": {"$lte": Int64(timestamp)}}]
        if timestamp >= idle_ttl:
            conditions.append({"last_seen": {"$lte": Int64(timestamp - idle_ttl)}})
        return self._delete_matching({"$or": conditions})


class MongoAttempts(_Repository):
    def get_many(self, keys: list[str]) -> dict[str, Record]:
        self._scope.check()
        if type(keys) is not list or any(
            not isinstance(key, str) or _HASH.fullmatch(key) is None for key in keys
        ):
            raise ValueError("Attempt keys must be canonical HMAC strings")
        with self._scope.collection("auth_attempts").find(
            {"_id": {"$in": keys}}, session=self._scope._session
        ) as cursor:
            records = [decode_record("auth_attempts", record) for record in cursor]
        return {record["key"]: record for record in records}

    def count(self) -> int:
        return self._scope.collection("auth_attempts").count_documents(
            {}, session=self._scope._session
        )

    def prune_expired(self, timestamp: int, *, window: int) -> int:
        self._scope.check(write=True)
        _timestamp(timestamp)
        if type(window) is not int or window <= 0:
            raise ValueError("Attempt window must be a positive integer")
        if timestamp < window:
            return 0
        result = self._scope.write(
            self._scope.collection("auth_attempts").delete_many,
            {"window_start": {"$lte": Int64(timestamp - window)}},
        )
        return result.deleted_count

    def set(self, attempt: Record) -> None:
        self._scope.check(write=True)
        encoded = encode_record("auth_attempts", attempt)
        identity = encoded["_id"]
        collection = self._scope.collection("auth_attempts")
        if self._scope.find("auth_attempts", {"_id": identity}, {"_id": 1}) is None:
            self._scope.write(collection.insert_one, encoded)
        else:
            result = self._scope.write(
                collection.replace_one, {"_id": identity}, encoded, upsert=False
            )
            if result.matched_count != 1:
                raise LookupError("Attempt identity does not exist")
        self._scope._dirty["auth_attempts"].add(identity)


class MongoReceipts(_Repository):
    def get(self, actor_id: str, key_hash: str) -> Record | None:
        self._scope.check()
        _identity(actor_id)
        if not isinstance(key_hash, str) or _HASH.fullmatch(key_hash) is None:
            raise ValueError("Receipt key must be a canonical digest")
        raw = self._scope.find("idempotency_records", {"actor_id": actor_id, "key_hash": key_hash})
        return decode_record("idempotency_records", raw) if raw is not None else None

    def count(self) -> int:
        return self._scope.collection("idempotency_records").count_documents(
            {}, session=self._scope._session
        )

    def prune_expired(self, timestamp: int) -> int:
        self._scope.check(write=True)
        _timestamp(timestamp)
        removed = 0
        cursor = self._scope.collection("idempotency_records").find(
            {"expires_at": {"$lte": Int64(timestamp)}}, {"_id": 1}, session=self._scope._session
        )
        with cursor:
            while batch := list(islice(cursor, 500)):
                identities = [record["_id"] for record in batch]
                result = self._scope.write(
                    self._scope.collection("idempotency_records").delete_many,
                    {"_id": {"$in": identities}},
                )
                if result.deleted_count != len(identities):
                    raise ValueError("Receipt deletion is inconsistent")
                self._scope.release(identities, "idempotency_records")
                removed += result.deleted_count
        return removed

    def insert(self, receipt: Record) -> None:
        self._insert("idempotency_records", receipt)


class MongoPersonal(_Repository):
    def get_owned(self, owner_id: str, identity: str) -> Record | None:
        self._scope.check()
        _identity(owner_id)
        _identity(identity)
        row = self._scope.find("collections", {"_id": identity, "owner_id": owner_id})
        return decode_record("collections", row) if row is not None else None

    def page(self, owner_id: str, query: PersonalQuery) -> tuple[list[Record], int]:
        self._scope.check()
        _identity(owner_id)
        if type(query) is not PersonalQuery:
            raise ValueError("A canonical personal query is required")
        selector = {"owner_id": owner_id}
        if query.kind:
            selector["kind"] = query.kind
        if query.name_key:
            selector["list_name_key"] = query.name_key
        collection = self._scope.collection("collections")
        total = collection.count_documents(selector, session=self._scope._session)
        with (
            collection.find(selector, session=self._scope._session)
            .sort([("created_at", -1), ("_id", -1)])
            .skip(query.offset)
            .limit(query.limit) as cursor
        ):
            return [decode_record("collections", row) for row in cursor], int(total)

    def has_duplicate(self, entry: Record, *, excluded_id: str | None = None) -> bool:
        self._scope.check()
        _identity(entry["owner_id"])
        _identity(entry["movie_id"])
        if not isinstance(entry.get("list_name"), str) or not isinstance(entry.get("kind"), str):
            raise ValueError("A canonical collection key is required")
        selector = {name: entry[name] for name in ("owner_id", "movie_id", "kind")}
        selector["list_name_key"] = entry["list_name"].casefold()
        if excluded_id is not None:
            _identity(excluded_id)
            selector["_id"] = {"$ne": excluded_id}
        return self._scope.find("collections", selector, {"_id": 1}) is not None

    def insert(self, entry: Record) -> None:
        self._insert("collections", entry)

    def save_owned(self, owner_id: str, entry: Record) -> None:
        self._scope.check(write=True)
        _identity(owner_id)
        encoded = encode_record("collections", entry)
        identity = encoded["_id"]
        selector = {"_id": identity, "owner_id": owner_id}
        if (
            entry["owner_id"] != owner_id
            or self._scope.find("collections", selector, {"_id": 1}) is None
        ):
            raise LookupError("Owned collection identity does not exist")
        self._scope.registered(identity, "collections")
        result = self._scope.write(
            self._scope.collection("collections").replace_one, selector, encoded, upsert=False
        )
        if result.matched_count != 1:
            raise LookupError("Owned collection identity does not exist")
        self._scope._dirty["collections"].add(identity)

    def delete_owned(self, owner_id: str, identity: str) -> bool:
        self._scope.check(write=True)
        _identity(owner_id)
        _identity(identity)
        selector = {"_id": identity, "owner_id": owner_id}
        if self._scope.find("collections", selector, {"_id": 1}) is None:
            return False
        self._scope.registered(identity, "collections")
        result = self._scope.write(self._scope.collection("collections").delete_one, selector)
        if result.deleted_count != 1:
            raise ValueError("Owned collection deletion is inconsistent")
        self._scope.release([identity], "collections")
        return True


class MongoEvents(_Repository):
    def __init__(self, scope):
        super().__init__(scope)
        self._tail_and_size: tuple[int, int] | None = None

    def append(self, event: Record) -> None:
        self.append_batch([event])

    def append_batch(self, events: list[Record]) -> None:
        self._scope.check(write=True)
        if not 1 <= len(events) <= 500:
            raise ValueError("An audit event batch must contain 1 to 500 events")
        collection = self._scope.collection("audit_events")
        tail, size = self._tail_and_size or (0, None)
        if size is None:
            last = collection.find_one(
                {}, {"_id": 0, "_ordinal": 1}, sort=[("_ordinal", -1)], session=self._scope._session
            )
            if last is not None:
                if type(last.get("_ordinal")) is not Int64 or not 1 <= last["_ordinal"]:
                    raise ValueError("Audit append ordinal is invalid or exhausted")
                tail = int(last["_ordinal"])
        ordinal = tail + len(events)
        if ordinal > INT64_MAX:
            raise ValueError("Audit append ordinal is invalid or exhausted")
        encoded = [
            encode_record("audit_events", event, audit_ordinal=tail + position)
            for position, event in enumerate(events, start=1)
        ]
        identities = [record["_id"] for record in encoded]
        claims = [{"_id": identity, "kind": "audit_events"} for identity in identities]
        self._scope.write(self._scope._database.entity_ids.insert_many, claims, ordered=True)
        self._scope.write(collection.insert_many, encoded, ordered=True)
        self._scope._dirty["audit_events"].update(identities)
        # A writable unit of work serializes writers and owns all changes to this journal.
        size = (
            collection.count_documents({}, session=self._scope._session)
            if size is None
            else size + len(events)
        )
        excess = size - AUDIT_KEEP
        if excess > 0:
            with (
                collection.find({}, {"_id": 1}, session=self._scope._session)
                .sort("_ordinal", 1)
                .limit(excess) as cursor
            ):
                while batch := list(islice(cursor, 500)):
                    identities = [row["_id"] for row in batch]
                    deleted = self._scope.write(
                        collection.delete_many, {"_id": {"$in": identities}}
                    )
                    if deleted.deleted_count != len(identities):
                        raise ValueError("Audit retention deletion is inconsistent")
                    self._scope.release(identities, "audit_events")
        self._tail_and_size = (ordinal, min(size, AUDIT_KEEP))

    def page(self, limit: int, offset: int) -> tuple[list[Record], int]:
        self._scope.check()
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise ValueError("Invalid audit page")
        collection = self._scope.collection("audit_events")
        total = collection.count_documents({}, session=self._scope._session)
        with (
            collection.find({}, session=self._scope._session)
            .sort("_ordinal", -1)
            .skip(offset)
            .limit(limit) as cursor
        ):
            return [decode_record("audit_events", row) for row in cursor], int(total)


class MongoCatalogue(_Repository):
    def import_movie_matches(self, keys):
        collection = self._scope.collection("movies")
        matches = {key: [] for key in keys}
        identity_fields = {"title": "$title_key", "year": "$year", "director": "$director_key"}
        for group in batched(matches, 100):
            selectors = [
                {"year": Int64(year), "title_key": title, "director_key": director}
                for title, year, director in group
            ]
            pipeline = [
                {"$match": {"$or": selectors}},
                # The identity of the first result matters only when there is exactly one.
                {
                    "$group": {
                        "_id": identity_fields,
                        "rows": {"$firstN": {"input": "$$ROOT", "n": 2}},
                    }
                },
            ]
            with collection.aggregate(pipeline, session=self._scope._session) as cursor:
                for row in cursor:
                    key = row["_id"]
                    matches[(key["title"], int(key["year"]), key["director"])] = [
                        decode_record("movies", movie) for movie in row["rows"]
                    ]
        return matches

    def occupied_import_ids(self, identities):
        self._scope.check()
        for identity in identities:
            _identity(identity)
        rows = self._scope.rows_by_ids(self._scope._database.entity_ids, identities, {"_id": 1})
        return {row["_id"] for row in rows}

    def import_copies(self, identities):
        for identity in identities:
            _identity(identity)
        rows = self._scope.rows_by_ids(self._scope.collection("copies"), identities)
        return {row["_id"]: decode_record("copies", row) for row in rows}

    def export_records(self):
        def records(name):
            with (
                self._scope.collection(name)
                .find({}, session=self._scope._session)
                .batch_size(_TRANSFER_BATCH) as cursor
            ):
                return [decode_record(name, row) for row in cursor]

        return records("movies"), records("copies")

    def insert_import(self, movies, copies):
        self._scope.check(write=True)
        prepared = [
            (name, [encode_record(name, record) for record in records])
            for name, records in (("movies", movies), ("copies", copies))
        ]
        for name, records in prepared:
            for group in batched(records, _TRANSFER_BATCH):
                claims = [{"_id": record["_id"], "kind": name} for record in group]
                self._scope.write(
                    self._scope._database.entity_ids.insert_many, claims, ordered=True
                )
                self._scope.write(
                    self._scope.collection(name).insert_many, list(group), ordered=True
                )
                self._scope._imported[name].update(claim["_id"] for claim in claims)

    def movies_by_ids(self, identities: list[str]) -> list[Record]:
        self._scope.check()
        if type(identities) is not list or len(identities) > 100:
            raise ValueError("Movie page identities must contain at most 100 items")
        for identity in identities:
            _identity(identity)
        if not identities:
            return []
        with self._scope.collection("movies").find(
            {"_id": {"$in": identities}}, session=self._scope._session
        ) as cursor:
            return [decode_record("movies", row) for row in cursor]

    def copies(self, movie_id: str | None = None) -> list[Record]:
        self._scope.check()
        if movie_id is not None:
            _identity(movie_id)
        selector = {} if movie_id is None else {"movie_id": movie_id}
        with self._scope.collection("copies").find(
            selector, session=self._scope._session
        ) as cursor:
            return [decode_record("copies", row) for row in cursor]

    def search_movies(self, query: MovieQuery) -> tuple[list[Record], int]:
        self._scope.check()
        if type(query) is not MovieQuery:
            raise ValueError("A canonical movie query is required")
        selector = {}
        if query.query:
            selector["search_key"] = {"$regex": re.escape(query.query).replace("\0", r"\x00")}
        if query.genre:
            selector["genre_keys"] = query.genre
        if query.year is not None:
            selector["year"] = Int64(query.year)
        direction = -1 if query.sort.startswith("-") else 1
        field = "title_key" if query.sort.lstrip("-") == "title" else "year"
        order = [(field, direction), ("_id", direction)]
        collection = self._scope.collection("movies")
        if not query.support:
            total = collection.count_documents(selector, session=self._scope._session)
            with (
                collection.find(selector, session=self._scope._session)
                .sort(order)
                .skip(query.offset)
                .limit(query.limit) as cursor
            ):
                return [decode_record("movies", row) for row in cursor], total
        pipeline = [
            {"$match": selector},
            {
                "$lookup": {
                    "from": "copies",
                    "localField": "_id",
                    "foreignField": "movie_id",
                    "pipeline": [
                        {"$match": {"support": query.support}},
                        {"$limit": 1},
                        {"$project": {"_id": 1}},
                    ],
                    "as": "_supported",
                }
            },
            {"$match": {"_supported.0": {"$exists": True}}},
            {"$project": {"_supported": 0}},
        ]
        with collection.aggregate(
            [*pipeline, {"$count": "total"}], session=self._scope._session
        ) as cursor:
            count = next(cursor, {"total": 0})["total"]
        with collection.aggregate(
            [*pipeline, {"$sort": dict(order)}, {"$skip": query.offset}, {"$limit": query.limit}],
            session=self._scope._session,
        ) as cursor:
            return [decode_record("movies", row) for row in cursor], int(count)

    def copy_summaries(self, movie_ids: list[str]) -> dict[str, Record]:
        self._scope.check()
        if type(movie_ids) is not list or len(movie_ids) > 100:
            raise ValueError("Movie summary page must contain at most 100 identities")
        for identity in movie_ids:
            _identity(identity)
        summaries = {identity: {"count": 0, "supports": []} for identity in movie_ids}
        if not movie_ids:
            return summaries
        pipeline = [
            {"$match": {"movie_id": {"$in": movie_ids}}},
            {
                "$group": {
                    "_id": {"movie": "$movie_id", "support": "$support"},
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"_id.movie": 1, "_id.support": 1}},
        ]
        with self._scope.collection("copies").aggregate(
            pipeline, session=self._scope._session
        ) as cursor:
            for row in cursor:
                summary = summaries[row["_id"]["movie"]]
                summary["count"] += int(row["count"])
                summary["supports"].append(row["_id"]["support"])
        return summaries

    def movie(self, identity: str) -> Record | None:
        return self._get("movies", identity)

    def copy(self, identity: str) -> Record | None:
        return self._get("copies", identity)

    def copies_for_movie(self, movie_id: str) -> list[Record]:
        self._scope.check()
        _identity(movie_id)
        with self._scope.collection("copies").find(
            {"movie_id": movie_id}, session=self._scope._session
        ) as cursor:
            return [decode_record("copies", record) for record in cursor]

    def has_duplicate_movie(self, movie: Record, *, excluded_id: str | None = None) -> bool:
        self._scope.check()
        if excluded_id is not None:
            _identity(excluded_id)
        if (
            type(movie) is not dict
            or not isinstance(movie.get("title"), str)
            or not isinstance(movie.get("director"), str)
            or type(movie.get("year")) is not int
            or not -(2**63) <= movie["year"] <= INT64_MAX
        ):
            raise ValueError("Movie key requires title, year and director")
        selector = {"year": Int64(movie["year"])}
        if excluded_id is not None:
            selector["_id"] = {"$ne": excluded_id}
        selector.update(
            title_key=movie["title"].casefold(), director_key=movie["director"].casefold()
        )
        return self._scope.find("movies", selector, {"_id": 1}) is not None

    def has_movie_references(self, movie_id: str) -> bool:
        self._scope.check()
        _identity(movie_id)
        return any(
            self._scope.find(name, {"movie_id": movie_id}, {"_id": 1}) is not None
            for name in ("copies", "collections", "loans", "reservations")
        )

    def has_copy_history(self, copy_id: str) -> bool:
        self._scope.check()
        _identity(copy_id)
        return self._scope.find("loans", {"copy_id": copy_id}, {"_id": 1}) is not None

    def insert_movie(self, movie: Record) -> None:
        self._insert("movies", movie)

    def save_movie(self, movie: Record) -> None:
        self._save("movies", movie)

    def delete_movie(self, identity: str) -> bool:
        return self._delete("movies", identity)

    def insert_copy(self, copy: Record) -> None:
        self._insert("copies", copy)

    def save_copy(self, copy: Record) -> None:
        self._save("copies", copy)

    def delete_copy(self, identity: str) -> bool:
        return self._delete("copies", identity)
