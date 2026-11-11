"""Targeted MongoDB repositories bound to one owned transaction/session."""

import re
from itertools import islice
from os import getpid
from threading import get_ident

from bson import Int64
from pymongo.errors import DuplicateKeyError, OperationFailure

from ..auth_storage import USERNAME
from ..storage_v2 import _valid_uuid, empty_document, validate_document
from .contracts import Record
from .mongo_model import INT64_MAX, collection_name, decode_record, encode_record

_HASH = re.compile(r"[0-9a-f]{64}\Z")


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
        self._dirty = {
            name: set() for name in ("users", "sessions", "movies", "copies", "auth_attempts")
        }
        self._deleted = {name: set() for name in ("movies", "copies")}

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

    def write(self, method, *args, **kwargs):
        self.check(write=True)
        try:
            return method(*args, session=self._session, **kwargs)
        except (DuplicateKeyError, OperationFailure) as exc:
            if isinstance(exc, DuplicateKeyError) or exc.code == 121:
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

    def validate(self):
        """Validate touched entities and their final references, never a DB snapshot."""
        self.check()
        for name, identities in self._dirty.items():
            for identity in identities:
                raw = self.find(name, {"_id": identity})
                if raw is None:
                    continue
                record = decode_record(name, raw)
                document = empty_document()
                document["revision"] = self._revision
                document[name] = [record]
                if name == "sessions":
                    parent = self.find("users", {"_id": record["user_id"]})
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


class MongoCatalogue(_Repository):
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
        with self._scope.collection("movies").find(
            selector, {"title": 1, "director": 1}, session=self._scope._session
        ) as cursor:
            return any(
                record["title"].casefold() == movie["title"].casefold()
                and record["director"].casefold() == movie["director"].casefold()
                for record in cursor
            )

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
