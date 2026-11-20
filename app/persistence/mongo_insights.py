"""Snapshot analytics over catalogue aggregates and actor-scoped projections."""

from bson import Int64

from ..insights import (
    ACTIVITY_WINDOW,
    Stock,
    catalogue_statistics,
    personal_statistics,
    rank_movies,
    with_availability,
)
from .mongo_model import decode_record
from .mongo_repositories import _identity, _timestamp


class MongoInsights:
    def __init__(self, scope):
        self._scope = scope

    def _aggregate(self, name, pipeline):
        collection = self._scope.collection(name)
        with collection.aggregate(pipeline, session=self._scope._session) as cursor:
            return list(cursor)

    def _find(self, name, selector=None, projection=None):
        collection = self._scope.collection(name)
        with collection.find(selector or {}, projection, session=self._scope._session) as cursor:
            yield from cursor

    def _count(self, name, selector):
        collection = self._scope.collection(name)
        return int(collection.count_documents(selector, session=self._scope._session))

    def _stock(self, timestamp, movie_ids=None):
        stock = Stock()
        if movie_ids == []:
            return stock
        selector = {} if movie_ids is None else {"movie_id": {"$in": movie_ids}}
        damaged = {"$eq": ["$condition", "damaged"]}
        rows = self._aggregate(
            "copies",
            [
                {"$match": selector},
                {
                    "$lookup": {
                        "from": "loans",
                        "localField": "_id",
                        "foreignField": "copy_id",
                        "pipeline": [
                            {"$match": {"status": "active"}},
                            {"$limit": 1},
                            {"$project": {"_id": 1}},
                        ],
                        "as": "_occupied",
                    }
                },
                {
                    "$group": {
                        "_id": {"movie": "$movie_id", "support": "$support"},
                        "total": {"$sum": 1},
                        "damaged": {"$sum": {"$cond": [damaged, 1, 0]}},
                        "free": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$not": [damaged]},
                                            {"$eq": [{"$size": "$_occupied"}, 0]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },
            ],
        )
        for row in rows:
            stock.supports[row["_id"]["support"]] += int(row["total"])
            stock.free[row["_id"]["movie"]] += int(row["free"])
            stock.damaged += int(row["damaged"])
        for row in self._aggregate(
            "reservations",
            [
                {"$match": {**selector, "status": "waiting", "expires_at": {"$gt": timestamp}}},
                {"$group": {"_id": "$movie_id", "total": {"$sum": 1}}},
            ],
        ):
            stock.waiting[row["_id"]] = int(row["total"])
        return stock

    def _entries(self, actor):
        return list(
            self._find(
                "collections",
                {"owner_id": actor},
                {"_id": 0, "movie_id": 1, "kind": 1, "supports": 1},
            )
        )

    def _loans(self, selector, timestamp):
        rows = self._aggregate(
            "loans",
            [
                {"$match": selector},
                {
                    "$group": {
                        "_id": "$status",
                        "total": {"$sum": 1},
                        "overdue": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$eq": ["$status", "active"]},
                                            {"$lt": ["$due_at", timestamp]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },
            ],
        )
        totals = {row["_id"]: int(row["total"]) for row in rows}
        return {
            "loans": {
                status: totals.get(status, 0)
                for status in ("pending", "active", "returned", "cancelled")
            },
            "overdue": sum(int(row["overdue"]) for row in rows),
        }

    def _activity(self, actor, period):
        selector = {"actor_id": actor}
        collection = self._scope.collection("audit_events")
        with (
            collection.find(
                selector, {"_id": 0, "action": 1, "timestamp": 1}, session=self._scope._session
            )
            .sort([("timestamp", -1), ("_id", -1)])
            .limit(10) as cursor
        ):
            recent = [{"action": e["action"], "timestamp": int(e["timestamp"])} for e in cursor]
        return {
            "activity_retained": self._count("audit_events", selector),
            "activity_last_7_days": self._count("audit_events", {**selector, "timestamp": period}),
            "recent_activity": recent,
        }

    def statistics(self, actor: str, timestamp: int, all_operations: bool) -> dict:
        self._scope.check()
        _identity(actor)
        _timestamp(timestamp)
        clock = Int64(timestamp)
        period = {"$gte": Int64(timestamp - ACTIVITY_WINDOW), "$lte": clock}
        stock = self._stock(clock)
        genres = [m["genres"] for m in self._find("movies", projection={"_id": 0, "genres": 1})]
        result = {
            "as_of": timestamp,
            "catalogue": catalogue_statistics(genres, stock),
            "personal": personal_statistics(
                self._entries(actor),
                self._loans({"borrower_id": actor}, clock),
                self._count(
                    "reservations",
                    {"user_id": actor, "status": "waiting", "expires_at": {"$gt": clock}},
                ),
                self._activity(actor, period),
            ),
        }
        if all_operations:
            activity = self._aggregate(
                "audit_events",
                [
                    {"$match": {"entity_type": {"$ne": "collection"}, "timestamp": period}},
                    {"$group": {"_id": "$action", "total": {"$sum": 1}}},
                    {"$sort": {"_id": 1}},
                ],
            )
            result["operations"] = {
                **self._loans({}, clock),
                "waiting_reservations": sum(stock.waiting.values()),
                "activity_last_7_days": {e["_id"]: int(e["total"]) for e in activity},
            }
        return result

    def recommendations(
        self, actor: str, timestamp: int, genres: tuple[str, ...], limit: int
    ) -> dict:
        self._scope.check()
        _identity(actor)
        _timestamp(timestamp)
        signals = self._aggregate(
            "loans",
            [
                {
                    "$match": {
                        "borrower_id": actor,
                        "status": {"$in": ["pending", "active", "returned"]},
                    }
                },
                {"$group": {"_id": {"movie_id": "$movie_id", "status": "$status"}}},
            ],
        )
        ranked = rank_movies(
            (decode_record("movies", row) for row in self._find("movies")),
            self._entries(actor),
            [row["_id"] for row in signals],
            genres,
            limit,
        )
        movie_ids = [entry["movie"]["id"] for entry in ranked["recommendations"]]
        return with_availability(ranked, self._stock(Int64(timestamp), movie_ids))
