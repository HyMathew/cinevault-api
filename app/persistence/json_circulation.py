"""Circulation selections over the owned, fully validated JSON document."""

import copy

from ..circulation_model import CirculationQuery
from .json_repositories import _identity, _Repository, _timestamp


class JsonCirculation(_Repository):
    def loan(self, identity, *, owner_id=None):
        return self._owned("loans", identity, owner_id)

    def reservation(self, identity, *, owner_id=None):
        return self._owned("reservations", identity, owner_id)

    def _owned(self, name, identity, owner_id):
        row = self._get(name, identity)
        if owner_id is not None:
            _identity(owner_id)
            owner = "borrower_id" if name == "loans" else "user_id"
            if row is not None and row[owner] != owner_id:
                return None
        return row

    def _page(self, name, owner_id, query):
        self._scope.check()
        if owner_id is not None:
            _identity(owner_id)
        if type(query) is not CirculationQuery:
            raise ValueError("A canonical circulation query is required")
        owner_key = "borrower_id" if name == "loans" else "user_id"
        rows = [
            row
            for row in self._scope.rows(name)
            if (owner_id is None or row[owner_key] == owner_id)
            and (not query.movie_id or row["movie_id"] == query.movie_id)
            and (
                not query.status
                or row["status"] == query.status
                or query.status == "open"
                and row["status"] in ("pending", "active")
            )
        ]
        rows.sort(
            key=(lambda row: (row["requested_at"], row["id"]))
            if name == "loans"
            else (lambda row: row["sequence"]),
            reverse=True,
        )
        return copy.deepcopy(rows[query.offset : query.offset + query.limit]), len(rows)

    def loans_page(self, owner_id, query):
        return self._page("loans", owner_id, query)

    def reservations_page(self, owner_id, query):
        return self._page("reservations", owner_id, query)

    def open_loan(self, borrower_id, movie_id):
        self._scope.check()
        _identity(borrower_id)
        _identity(movie_id)
        return next(
            (
                copy.deepcopy(row)
                for row in self._scope.rows("loans")
                if row["borrower_id"] == borrower_id
                and row["movie_id"] == movie_id
                and row["status"] in ("pending", "active")
            ),
            None,
        )

    def count_loans(self, borrower_id, status):
        self._scope.check()
        _identity(borrower_id)
        if status not in ("pending", "active"):
            raise ValueError("Invalid open-loan count")
        return sum(
            row["borrower_id"] == borrower_id and row["status"] == status
            for row in self._scope.rows("loans")
        )

    def active_for_copy(self, copy_id):
        self._scope.check()
        _identity(copy_id)
        return any(
            row["copy_id"] == copy_id and row["status"] == "active"
            for row in self._scope.rows("loans")
        )

    def count_waiting_for_user(self, user_id):
        self._scope.check()
        _identity(user_id)
        return sum(
            row["user_id"] == user_id and row["status"] == "waiting"
            for row in self._scope.rows("reservations")
        )

    def queue_facts(self, movie_id, timestamp, *, user_id=None, before_sequence=None):
        self._scope.check()
        _identity(movie_id)
        _timestamp(timestamp)
        if user_id is not None:
            _identity(user_id)
        if before_sequence is not None and (
            type(before_sequence) is not int or before_sequence < 1
        ):
            raise ValueError("Invalid queue position")
        rows = sorted(
            (
                row
                for row in self._scope.rows("reservations")
                if row["movie_id"] == movie_id
                and row["status"] == "waiting"
                and row["expires_at"] > timestamp
            ),
            key=lambda row: row["sequence"],
        )
        return {
            "total": len(rows),
            "head": copy.deepcopy(rows[0]) if rows else None,
            "owned": next((copy.deepcopy(row) for row in rows if row["user_id"] == user_id), None),
            "preceding": sum(row["sequence"] < before_sequence for row in rows)
            if before_sequence is not None
            else 0,
        }

    def open_loans_for_pairs(self, pairs):
        self._scope.check()
        if type(pairs) is not list or len(pairs) > 100:
            raise ValueError("At most 100 page pairs are required")
        for user, movie in pairs:
            _identity(user)
            _identity(movie)
        selected = set(pairs)
        return copy.deepcopy(
            [
                row
                for row in self._scope.rows("loans")
                if (row["borrower_id"], row["movie_id"]) in selected
                and row["status"] in ("pending", "active")
            ]
        )

    def free_copies(self, movie_id):
        self._scope.check()
        _identity(movie_id)
        occupied = {
            row["copy_id"]
            for row in self._scope.rows("loans")
            if row["status"] == "active" and row["movie_id"] == movie_id
        }
        return copy.deepcopy(
            [
                row
                for row in self._scope.rows("copies")
                if row["movie_id"] == movie_id
                and row["condition"] != "damaged"
                and row["id"] not in occupied
            ]
        )

    def inventory_counts(self, movie_id):
        self._scope.check()
        _identity(movie_id)
        return {
            "total": sum(row["movie_id"] == movie_id for row in self._scope.rows("copies")),
            "loaned": sum(
                row["movie_id"] == movie_id and row["status"] == "active"
                for row in self._scope.rows("loans")
            ),
            "free": len(self.free_copies(movie_id)),
        }

    def _batches(self, selector, batch_size, *, sequence_order=True):
        self._scope.check()
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("Circulation batches contain at most 500 rows")
        rows = [row for row in self._scope.rows("reservations") if selector(row)]
        if sequence_order:
            rows.sort(key=lambda row: row["sequence"])
        for offset in range(0, len(rows), batch_size):
            self._scope.check()
            yield copy.deepcopy(rows[offset : offset + batch_size])
        self._scope.check()

    def expired_batches(self, timestamp, batch_size=500):
        self._scope.check()
        _timestamp(timestamp)
        return self._batches(
            lambda row: row["status"] == "waiting" and row["expires_at"] <= timestamp, batch_size
        )

    def waiting_user_batches(self, user_id, batch_size=500):
        self._scope.check()
        _identity(user_id)
        return self._batches(
            lambda row: row["status"] == "waiting" and row["user_id"] == user_id,
            batch_size,
            sequence_order=False,
        )

    def insert_loan(self, loan):
        self._insert("loans", loan)

    def save_loan(self, loan, *, owner_id=None):
        if owner_id is not None and (
            loan["borrower_id"] != owner_id or self.loan(loan["id"], owner_id=owner_id) is None
        ):
            raise LookupError("Owned loan does not exist")
        self._save("loans", loan)

    def insert_reservation(self, reservation):
        self._insert("reservations", reservation)

    def save_reservation(self, reservation, *, owner_id=None):
        if owner_id is not None and (
            reservation["user_id"] != owner_id
            or self.reservation(reservation["id"], owner_id=owner_id) is None
        ):
            raise LookupError("Owned reservation does not exist")
        self._save("reservations", reservation)
