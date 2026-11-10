"""Targeted JSON access; only the unit of work owns the mutable document."""

import copy
import re
from os import getpid
from threading import get_ident

from ..auth_storage import USERNAME
from ..storage_v2 import COLLECTIONS, _valid_uuid
from .contracts import Record

_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _identity(value):
    if not _valid_uuid(value):
        raise ValueError("Identity must be a canonical UUID string")


def _timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError("Timestamp must be a non-negative integer")


class _DocumentScope:
    def __init__(self, document, *, writable):
        self._document = document
        self._writable = writable
        self._owner = (getpid(), get_ident())

    def check(self, *, write=False):
        if self._document is None:
            raise RuntimeError("Unit of work is closed")
        if self._owner != (getpid(), get_ident()):
            raise RuntimeError("Unit of work must stay with its owner")
        if write and not self._writable:
            raise RuntimeError("Query unit of work is read-only")

    def rows(self, name, *, write=False):
        self.check(write=write)
        if name not in COLLECTIONS:
            raise ValueError("Unknown persistence collection")
        return self._document[name]

    def close(self):
        self._document = None


class _Repository:
    def __init__(self, scope):
        self._scope = scope

    def _get(self, name, identity):
        self._scope.check()
        _identity(identity)
        return next(
            (copy.deepcopy(row) for row in self._scope.rows(name) if row["id"] == identity),
            None,
        )

    def _insert(self, name, record):
        self._scope.check(write=True)
        candidate = self._record(record)
        rows = self._scope.rows(name, write=True)
        if any(row["id"] == candidate["id"] for row in rows):
            raise ValueError("Persistence identity already exists")
        rows.append(candidate)

    def _save(self, name, record):
        self._scope.check(write=True)
        candidate = self._record(record)
        rows = self._scope.rows(name, write=True)
        for index, row in enumerate(rows):
            if row["id"] == candidate["id"]:
                rows[index] = candidate
                return
        raise LookupError("Persistence identity does not exist")

    def _delete(self, name, identity):
        self._scope.check(write=True)
        _identity(identity)
        rows = self._scope.rows(name, write=True)
        for index, row in enumerate(rows):
            if row["id"] == identity:
                del rows[index]
                return True
        return False

    @staticmethod
    def _record(record):
        if type(record) is not dict or "id" not in record:
            raise ValueError("Entity requires an identity")
        _identity(record["id"])
        return copy.deepcopy(record)


class JsonAccounts(_Repository):
    def get(self, identity: str) -> Record | None:
        return self._get("users", identity)

    def by_username(self, username: str) -> Record | None:
        self._scope.check()
        if not isinstance(username, str) or USERNAME.fullmatch(username) is None:
            raise ValueError("Username must be canonical")
        return next(
            (
                copy.deepcopy(row)
                for row in self._scope.rows("users")
                if row["username"] == username
            ),
            None,
        )

    def page(self, limit: int, offset: int) -> list[Record]:
        self._scope.check()
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise ValueError("Invalid account page")
        rows = sorted(self._scope.rows("users"), key=lambda row: row["username"])
        return copy.deepcopy(rows[offset : offset + limit])

    def has_other_enabled_admin(self, identity: str) -> bool:
        self._scope.check()
        _identity(identity)
        return any(
            row["id"] != identity and row["role"] == "admin" and row["enabled"]
            for row in self._scope.rows("users")
        )

    def insert(self, account: Record) -> None:
        self._insert("users", account)

    def save(self, account: Record) -> None:
        self._save("users", account)


class JsonSessions(_Repository):
    def by_token_hash(self, token_hash: str) -> Record | None:
        self._scope.check()
        if not isinstance(token_hash, str) or _HASH.fullmatch(token_hash) is None:
            raise ValueError("Token hash must be canonical")
        return next(
            (
                copy.deepcopy(row)
                for row in self._scope.rows("sessions")
                if row["token_hash"] == token_hash
            ),
            None,
        )

    def touch(self, identity: str, timestamp: int) -> None:
        self._scope.check(write=True)
        _identity(identity)
        _timestamp(timestamp)
        rows = self._scope.rows("sessions", write=True)
        for row in rows:
            if row["id"] == identity:
                row["last_seen"] = timestamp
                return
        raise LookupError("Persistence identity does not exist")

    def insert(self, session: Record) -> None:
        self._insert("sessions", session)

    def revoke_user(self, user_id: str) -> int:
        self._scope.check(write=True)
        _identity(user_id)
        rows = self._scope.rows("sessions", write=True)
        kept = [row for row in rows if row["user_id"] != user_id]
        removed = len(rows) - len(kept)
        rows[:] = kept
        return removed

    def revoke(self, identity: str) -> bool:
        return self._delete("sessions", identity)

    def delete_expired(self, timestamp: int, *, idle_ttl: int) -> int:
        self._scope.check(write=True)
        _timestamp(timestamp)
        if type(idle_ttl) is not int or idle_ttl <= 0:
            raise ValueError("Idle lifetime must be a positive integer")
        rows = self._scope.rows("sessions", write=True)
        kept = [
            row
            for row in rows
            if row["expires_at"] > timestamp and row["last_seen"] + idle_ttl > timestamp
        ]
        removed = len(rows) - len(kept)
        rows[:] = kept
        return removed


class JsonCatalogue(_Repository):
    def movie(self, identity: str) -> Record | None:
        return self._get("movies", identity)

    def copy(self, identity: str) -> Record | None:
        return self._get("copies", identity)

    def copies_for_movie(self, movie_id: str) -> list[Record]:
        self._scope.check()
        _identity(movie_id)
        return copy.deepcopy(
            [row for row in self._scope.rows("copies") if row["movie_id"] == movie_id]
        )

    def has_duplicate_movie(self, movie: Record, *, excluded_id: str | None = None) -> bool:
        self._scope.check()
        if excluded_id is not None:
            _identity(excluded_id)
        if (
            type(movie) is not dict
            or not isinstance(movie.get("title"), str)
            or not isinstance(movie.get("director"), str)
            or type(movie.get("year")) is not int
        ):
            raise ValueError("Movie key requires title, year and director")
        key = (movie["title"].casefold(), movie["year"], movie["director"].casefold())
        return any(
            row["id"] != excluded_id
            and (row["title"].casefold(), row["year"], row["director"].casefold()) == key
            for row in self._scope.rows("movies")
        )

    def has_movie_references(self, movie_id: str) -> bool:
        self._scope.check()
        _identity(movie_id)
        return any(
            row["movie_id"] == movie_id
            for name in ("copies", "collections", "loans", "reservations")
            for row in self._scope.rows(name)
        )

    def has_copy_history(self, copy_id: str) -> bool:
        self._scope.check()
        _identity(copy_id)
        return any(row["copy_id"] == copy_id for row in self._scope.rows("loans"))

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
