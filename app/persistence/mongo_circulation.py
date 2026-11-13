"""Targeted circulation queries bound to the existing owned MongoDB UoW."""

from itertools import islice

from bson import Int64

from ..circulation_model import CirculationQuery
from .mongo_model import decode_record, encode_record
from .mongo_repositories import _identity, _Repository, _timestamp


class MongoCirculation(_Repository):
    def loan(self, identity, *, owner_id=None):
        return self._owned("loans", identity, owner_id)

    def reservation(self, identity, *, owner_id=None):
        return self._owned("reservations", identity, owner_id)

    def _owned(self, name, identity, owner_id):
        self._scope.check()
        _identity(identity)
        selector = {"_id": identity}
        if owner_id is not None:
            _identity(owner_id)
            selector["borrower_id" if name == "loans" else "user_id"] = owner_id
        raw = self._scope.find(name, selector)
        return decode_record(name, raw) if raw is not None else None

    def _owned_save(self, name, record, owner_id):
        self._scope.check(write=True)
        _identity(owner_id)
        owner = "borrower_id" if name == "loans" else "user_id"
        encoded = encode_record(name, record)
        identity = encoded["_id"]
        selector = {"_id": identity, owner: owner_id}
        if record[owner] != owner_id or self._scope.find(name, selector, {"_id": 1}) is None:
            raise LookupError("Owned circulation identity does not exist")
        self._scope.registered(identity, name)
        result = self._scope.write(
            self._scope.collection(name).replace_one, selector, encoded, upsert=False
        )
        if result.matched_count != 1:
            raise LookupError("Owned circulation identity does not exist")
        self._scope._dirty[name].add(identity)

    def _page(self, name, owner_id, query):
        self._scope.check()
        if type(query) is not CirculationQuery:
            raise ValueError("A canonical circulation query is required")
        selector = {}
        if owner_id is not None:
            _identity(owner_id)
            selector["borrower_id" if name == "loans" else "user_id"] = owner_id
        if query.movie_id:
            selector["movie_id"] = query.movie_id
        if query.status:
            selector["status"] = (
                {"$in": ["pending", "active"]} if query.status == "open" else query.status
            )
        collection = self._scope.collection(name)
        total = collection.count_documents(selector, session=self._scope._session)
        order = [("requested_at", -1), ("_id", -1)] if name == "loans" else [("sequence", -1)]
        with (
            collection.find(selector, session=self._scope._session)
            .sort(order)
            .skip(query.offset)
            .limit(query.limit) as cursor
        ):
            return [decode_record(name, row) for row in cursor], int(total)

    def loans_page(self, owner_id, query):
        return self._page("loans", owner_id, query)

    def reservations_page(self, owner_id, query):
        return self._page("reservations", owner_id, query)

    def open_loan(self, borrower_id, movie_id):
        self._scope.check()
        _identity(borrower_id)
        _identity(movie_id)
        row = self._scope.find(
            "loans",
            {
                "borrower_id": borrower_id,
                "movie_id": movie_id,
                "status": {"$in": ["pending", "active"]},
            },
        )
        return decode_record("loans", row) if row is not None else None

    def count_loans(self, borrower_id, status):
        self._scope.check()
        _identity(borrower_id)
        if status not in ("pending", "active"):
            raise ValueError("Invalid open-loan count")
        return self._scope.collection("loans").count_documents(
            {"borrower_id": borrower_id, "status": status},
            session=self._scope._session,
        )

    def active_for_copy(self, copy_id):
        self._scope.check()
        _identity(copy_id)
        return (
            self._scope.find("loans", {"copy_id": copy_id, "status": "active"}, {"_id": 1})
            is not None
        )

    def count_waiting_for_user(self, user_id):
        self._scope.check()
        _identity(user_id)
        return self._scope.collection("reservations").count_documents(
            {"user_id": user_id, "status": "waiting"},
            session=self._scope._session,
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
        selector = {
            "movie_id": movie_id,
            "status": "waiting",
            "expires_at": {"$gt": Int64(timestamp)},
        }
        collection = self._scope.collection("reservations")
        total = collection.count_documents(selector, session=self._scope._session)
        head = collection.find_one(selector, sort=[("sequence", 1)], session=self._scope._session)
        owned = (
            collection.find_one({**selector, "user_id": user_id}, session=self._scope._session)
            if user_id is not None
            else None
        )
        preceding = (
            collection.count_documents(
                {**selector, "sequence": {"$lt": Int64(before_sequence)}},
                session=self._scope._session,
            )
            if before_sequence is not None
            else 0
        )
        return {
            "total": int(total),
            "head": decode_record("reservations", head) if head else None,
            "owned": decode_record("reservations", owned) if owned else None,
            "preceding": int(preceding),
        }

    def open_loans_for_pairs(self, pairs):
        self._scope.check()
        if type(pairs) is not list or len(pairs) > 100:
            raise ValueError("At most 100 page pairs are required")
        for user, movie in pairs:
            _identity(user)
            _identity(movie)
        if not pairs:
            return []
        selector = {
            "status": {"$in": ["pending", "active"]},
            "$or": [{"borrower_id": user, "movie_id": movie} for user, movie in pairs],
        }
        with self._scope.collection("loans").find(selector, session=self._scope._session) as cursor:
            return [decode_record("loans", row) for row in cursor]

    @staticmethod
    def _free_pipeline(movie_id):
        return [
            {"$match": {"movie_id": movie_id, "condition": {"$ne": "damaged"}}},
            {
                "$lookup": {
                    "from": "loans",
                    "let": {"copy": "$_id"},
                    "pipeline": [
                        {"$match": {"status": "active", "$expr": {"$eq": ["$copy_id", "$$copy"]}}},
                        {"$limit": 1},
                        {"$project": {"_id": 1}},
                    ],
                    "as": "_occupied",
                }
            },
            {"$match": {"_occupied": {"$size": 0}}},
        ]

    def free_copies(self, movie_id):
        self._scope.check()
        _identity(movie_id)
        pipeline = [
            *self._free_pipeline(movie_id),
            {"$project": {"_occupied": 0}},
            {"$sort": {"_id": 1}},
        ]
        with self._scope.collection("copies").aggregate(
            pipeline, session=self._scope._session
        ) as cursor:
            return [decode_record("copies", row) for row in cursor]

    def inventory_counts(self, movie_id):
        self._scope.check()
        _identity(movie_id)
        total = self._scope.collection("copies").count_documents(
            {"movie_id": movie_id}, session=self._scope._session
        )
        loaned = self._scope.collection("loans").count_documents(
            {"movie_id": movie_id, "status": "active"}, session=self._scope._session
        )
        with self._scope.collection("copies").aggregate(
            [*self._free_pipeline(movie_id), {"$count": "count"}],
            session=self._scope._session,
        ) as cursor:
            counts = list(cursor)
        return {
            "total": int(total),
            "loaned": int(loaned),
            "free": int(counts[0]["count"]) if counts else 0,
        }

    def _batches(self, selector, batch_size):
        self._scope.check()
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("Circulation batches contain at most 500 rows")
        with (
            self._scope.collection("reservations")
            .find(
                selector,
                session=self._scope._session,
            )
            .sort("sequence", 1)
            .batch_size(batch_size) as cursor
        ):
            while True:
                self._scope.check()
                batch = list(islice(cursor, batch_size))
                if not batch:
                    break
                yield [decode_record("reservations", row) for row in batch]
            self._scope.check()

    def expired_batches(self, timestamp, batch_size=500):
        self._scope.check()
        _timestamp(timestamp)
        return self._batches(
            {"status": "waiting", "expires_at": {"$lte": Int64(timestamp)}}, batch_size
        )

    def waiting_user_batches(self, user_id, batch_size=500):
        self._scope.check()
        _identity(user_id)
        return self._batches({"status": "waiting", "user_id": user_id}, batch_size)

    def insert_loan(self, loan):
        self._insert("loans", loan)

    def save_loan(self, loan, *, owner_id=None):
        if owner_id is None:
            self._save("loans", loan)
        else:
            self._owned_save("loans", loan, owner_id)

    def insert_reservation(self, reservation):
        self._insert("reservations", reservation)

    def save_reservation(self, reservation, *, owner_id=None):
        if owner_id is None:
            self._save("reservations", reservation)
        else:
            self._owned_save("reservations", reservation, owner_id)
