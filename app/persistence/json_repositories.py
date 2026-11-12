"""Targeted JSON access; only the unit of work owns the mutable document."""

import copy
import re
from os import getpid
from threading import get_ident

from ..auth_storage import USERNAME
from ..catalogue_query import MovieQuery, movie_keys
from ..personal_model import AUDIT_KEEP, PersonalQuery
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


class JsonAttempts(_Repository):
    def get_many(self, keys: list[str]) -> dict[str, Record]:
        self._scope.check()
        if type(keys) is not list or any(
            not isinstance(key, str) or _HASH.fullmatch(key) is None for key in keys
        ):
            raise ValueError("Attempt keys must be canonical HMAC strings")
        selected = set(keys)
        return {
            row["key"]: copy.deepcopy(row)
            for row in self._scope.rows("auth_attempts")
            if row["key"] in selected
        }

    def count(self) -> int:
        return len(self._scope.rows("auth_attempts"))

    def prune_expired(self, timestamp: int, *, window: int) -> int:
        self._scope.check(write=True)
        _timestamp(timestamp)
        if type(window) is not int or window <= 0:
            raise ValueError("Attempt window must be a positive integer")
        rows = self._scope.rows("auth_attempts", write=True)
        retained = [row for row in rows if timestamp - row["window_start"] < window]
        removed = len(rows) - len(retained)
        rows[:] = retained
        return removed

    def set(self, attempt: Record) -> None:
        self._scope.check(write=True)
        if (
            type(attempt) is not dict
            or not isinstance(attempt.get("key"), str)
            or _HASH.fullmatch(attempt["key"]) is None
        ):
            raise ValueError("Attempt identity must be a canonical HMAC string")
        candidate = copy.deepcopy(attempt)
        rows = self._scope.rows("auth_attempts", write=True)
        for index, row in enumerate(rows):
            if row["key"] == candidate["key"]:
                rows[index] = candidate
                return
        rows.append(candidate)


class JsonReceipts(_Repository):
    def get(self, actor_id: str, key_hash: str) -> Record | None:
        self._scope.check()
        _identity(actor_id)
        if not isinstance(key_hash, str) or _HASH.fullmatch(key_hash) is None:
            raise ValueError("Receipt key must be a canonical digest")
        return next(
            (
                copy.deepcopy(row)
                for row in self._scope.rows("idempotency_records")
                if row["actor_id"] == actor_id and row["key_hash"] == key_hash
            ),
            None,
        )

    def count(self) -> int:
        return len(self._scope.rows("idempotency_records"))

    def prune_expired(self, timestamp: int) -> int:
        self._scope.check(write=True)
        _timestamp(timestamp)
        rows = self._scope.rows("idempotency_records", write=True)
        retained = [row for row in rows if row["expires_at"] > timestamp]
        removed = len(rows) - len(retained)
        rows[:] = retained
        return removed

    def insert(self, receipt: Record) -> None:
        self._insert("idempotency_records", receipt)


class JsonPersonal(_Repository):
    def get_owned(self, owner_id: str, identity: str) -> Record | None:
        self._scope.check()
        _identity(owner_id)
        item = self._get("collections", identity)
        return item if item is not None and item["owner_id"] == owner_id else None

    def page(self, owner_id: str, query: PersonalQuery) -> tuple[list[Record], int]:
        self._scope.check()
        _identity(owner_id)
        if type(query) is not PersonalQuery:
            raise ValueError("A canonical personal query is required")
        rows = [
            item
            for item in self._scope.rows("collections")
            if item["owner_id"] == owner_id
            and (not query.kind or item["kind"] == query.kind)
            and (not query.name_key or item["list_name"].casefold() == query.name_key)
        ]
        rows.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return copy.deepcopy(rows[query.offset : query.offset + query.limit]), len(rows)

    def has_duplicate(self, entry: Record, *, excluded_id: str | None = None) -> bool:
        self._scope.check()
        _identity(entry["owner_id"])
        _identity(entry["movie_id"])
        if excluded_id is not None:
            _identity(excluded_id)
        if not isinstance(entry.get("list_name"), str) or not isinstance(entry.get("kind"), str):
            raise ValueError("A canonical collection key is required")
        return any(
            row["id"] != excluded_id
            and all(row[key] == entry[key] for key in ("owner_id", "movie_id", "kind"))
            and row["list_name"].casefold() == entry["list_name"].casefold()
            for row in self._scope.rows("collections")
        )

    def insert(self, entry: Record) -> None:
        self._insert("collections", entry)

    def save_owned(self, owner_id: str, entry: Record) -> None:
        self._scope.check(write=True)
        _identity(owner_id)
        if entry.get("owner_id") != owner_id or self.get_owned(owner_id, entry["id"]) is None:
            raise LookupError("Owned collection identity does not exist")
        self._save("collections", entry)

    def delete_owned(self, owner_id: str, identity: str) -> bool:
        self._scope.check(write=True)
        return (
            self._delete("collections", identity)
            if self.get_owned(owner_id, identity) is not None
            else False
        )


class JsonEvents(_Repository):
    def append(self, event: Record) -> None:
        self._insert("audit_events", event)
        rows = self._scope.rows("audit_events", write=True)
        rows[:] = rows[-AUDIT_KEEP:]

    def page(self, limit: int, offset: int) -> tuple[list[Record], int]:
        self._scope.check()
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise ValueError("Invalid audit page")
        rows = self._scope.rows("audit_events")
        return copy.deepcopy(list(reversed(rows))[offset : offset + limit]), len(rows)


class JsonCatalogue(_Repository):
    def movies_by_ids(self, identities: list[str]) -> list[Record]:
        self._scope.check()
        if type(identities) is not list or len(identities) > 100:
            raise ValueError("Movie page identities must contain at most 100 items")
        for identity in identities:
            _identity(identity)
        selected = set(identities)
        return copy.deepcopy([row for row in self._scope.rows("movies") if row["id"] in selected])

    def copies(self, movie_id: str | None = None) -> list[Record]:
        self._scope.check()
        if movie_id is not None:
            _identity(movie_id)
        return copy.deepcopy(
            [
                row
                for row in self._scope.rows("copies")
                if movie_id is None or row["movie_id"] == movie_id
            ]
        )

    def search_movies(self, query: MovieQuery) -> tuple[list[Record], int]:
        self._scope.check()
        if type(query) is not MovieQuery:
            raise ValueError("A canonical movie query is required")
        supported = {
            row["movie_id"] for row in self._scope.rows("copies") if row["support"] == query.support
        }
        rows = []
        for row in self._scope.rows("movies"):
            keys = movie_keys(row)
            if (
                (not query.query or query.query in keys["search_key"])
                and (not query.genre or query.genre in keys["genre_keys"])
                and (query.year is None or query.year == row["year"])
                and (not query.support or row["id"] in supported)
            ):
                rows.append(row)
        field = query.sort.lstrip("-")
        rows.sort(
            key=lambda row: (row[field].casefold() if field == "title" else row[field], row["id"]),
            reverse=query.sort.startswith("-"),
        )
        return copy.deepcopy(rows[query.offset : query.offset + query.limit]), len(rows)

    def copy_summaries(self, movie_ids: list[str]) -> dict[str, Record]:
        self._scope.check()
        if type(movie_ids) is not list or len(movie_ids) > 100:
            raise ValueError("Movie summary page must contain at most 100 identities")
        for identity in movie_ids:
            _identity(identity)
        summaries = {identity: {"count": 0, "supports": []} for identity in movie_ids}
        for row in self._scope.rows("copies"):
            if row["movie_id"] in summaries:
                summary = summaries[row["movie_id"]]
                summary["count"] += 1
                if row["support"] not in summary["supports"]:
                    summary["supports"].append(row["support"])
        return summaries

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
