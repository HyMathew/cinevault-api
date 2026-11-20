"""Derived analytics on one snapshot; no cross-user preference aggregation."""

from collections import Counter
from dataclasses import dataclass, field
from heapq import nsmallest

from .business_storage import KINDS
from .services.catalogue_model import SUPPORTS, resource


@dataclass
class Stock:
    supports: Counter = field(default_factory=Counter)
    free: Counter = field(default_factory=Counter)
    waiting: Counter = field(default_factory=Counter)
    damaged: int = 0

    def available(self, movie_id):
        return max(0, self.free[movie_id] - self.waiting[movie_id])


def inventory(document, timestamp):
    result = Stock()
    occupied = {loan["copy_id"] for loan in document["loans"] if loan["status"] == "active"}
    for entry in document["copies"]:
        result.supports[entry["support"]] += 1
        damaged = entry["condition"] == "damaged"
        result.damaged += damaged
        result.free[entry["movie_id"]] += not damaged and entry["id"] not in occupied
    for reservation in document["reservations"]:
        if reservation["status"] == "waiting" and reservation["expires_at"] > timestamp:
            result.waiting[reservation["movie_id"]] += 1
    return result


def loan_counts(loans, timestamp):
    return {
        "loans": {
            status: sum(loan["status"] == status for loan in loans)
            for status in ("pending", "active", "returned", "cancelled")
        },
        "overdue": sum(loan["status"] == "active" and loan["due_at"] < timestamp for loan in loans),
    }


def catalogue_statistics(genres, stock):
    genre_counts = Counter()
    genre_labels = {}
    for movie_genres in genres:
        for genre in movie_genres:
            key = genre.casefold()
            genre_counts[key] += 1
            genre_labels[key] = min(genre_labels.get(key, genre), genre)
    free = sum(stock.free.values())
    held = sum(min(count, stock.waiting[movie_id]) for movie_id, count in stock.free.items())
    return {
        "movies": len(genres),
        "copies": sum(stock.supports.values()),
        "by_support": {support: stock.supports[support] for support in SUPPORTS},
        "by_genre": {genre_labels[key]: count for key, count in sorted(genre_counts.items())},
        "damaged": stock.damaged,
        "free": free,
        "held_for_queue": held,
        "available": free - held,
    }


def personal_statistics(entries, loans, waiting, activity):
    return {
        "collection_entries": len(entries),
        "collection_movies": len({e["movie_id"] for e in entries}),
        "by_kind": {kind: sum(e["kind"] == kind for e in entries) for kind in KINDS},
        "by_owned_support": {
            support: sum(e["kind"] == "owned" and support in e["supports"] for e in entries)
            for support in SUPPORTS
        },
        **loans,
        "waiting_reservations": waiting,
        **activity,
    }


ACTIVITY_WINDOW = 7 * 86400


def statistics(document, user_id, timestamp, all_operations=False):
    stock = inventory(document, timestamp)
    entries = [entry for entry in document["collections"] if entry["owner_id"] == user_id]
    own_loans = [loan for loan in document["loans"] if loan["borrower_id"] == user_id]
    events = [event for event in document["audit_events"] if event["actor_id"] == user_id]
    recent = [
        event for event in events if timestamp - ACTIVITY_WINDOW <= event["timestamp"] <= timestamp
    ]
    own_reservations = [r for r in document["reservations"] if r["user_id"] == user_id]
    result = {
        "as_of": timestamp,
        "catalogue": catalogue_statistics([movie["genres"] for movie in document["movies"]], stock),
        "personal": personal_statistics(
            entries,
            loan_counts(own_loans, timestamp),
            sum(r["status"] == "waiting" and r["expires_at"] > timestamp for r in own_reservations),
            {
                "activity_retained": len(events),
                "activity_last_7_days": len(recent),
                "recent_activity": [
                    {"action": e["action"], "timestamp": e["timestamp"]}
                    for e in sorted(events, key=lambda e: (e["timestamp"], e["id"]), reverse=True)[
                        :10
                    ]
                ],
            },
        ),
    }
    if all_operations:
        operations_events = [
            e
            for e in document["audit_events"]
            if e["entity_type"] != "collection"
            and timestamp - ACTIVITY_WINDOW <= e["timestamp"] <= timestamp
        ]
        result["operations"] = {
            **loan_counts(document["loans"], timestamp),
            "waiting_reservations": sum(stock.waiting.values()),
            "activity_last_7_days": dict(
                sorted(Counter(e["action"] for e in operations_events).items())
            ),
        }
    return result


def recommendations(document, user_id, timestamp, preferred=(), limit=12):
    entries = [entry for entry in document["collections"] if entry["owner_id"] == user_id]
    loans = [loan for loan in document["loans"] if loan["borrower_id"] == user_id]
    return with_availability(
        rank_movies(document["movies"], entries, loans, preferred, limit),
        inventory(document, timestamp),
    )


def rank_movies(catalogue, entries, loans, preferred, limit):
    movies = {movie["id"]: movie for movie in catalogue}
    seeds = {}
    excluded = set()
    watchlist = set()
    for entry in entries:
        movie_id = entry["movie_id"]
        if entry["kind"] in ("favorite", "watched"):
            seeds[movie_id] = max(seeds.get(movie_id, 0), 3 if entry["kind"] == "favorite" else 1)
        if entry["kind"] in ("favorite", "watched", "owned"):
            excluded.add(movie_id)
        if entry["kind"] == "watchlist":
            watchlist.add(movie_id)
    for loan in loans:
        if loan["status"] in ("pending", "active", "returned"):
            excluded.add(loan["movie_id"])
        if loan["status"] == "returned":
            seeds[loan["movie_id"]] = max(seeds.get(loan["movie_id"], 0), 1)
    genres = Counter()
    directors = set()
    actors = set()
    for identity, weight in seeds.items():
        movie = movies[identity]
        for genre in movie["genres"]:
            genres[genre.casefold()] += weight
        directors.add(movie["director"].casefold())
        actors.update(actor.casefold() for actor in movie["actors"])
    preferred = {genre.casefold() for genre in preferred}

    def rank(movie):
        reasons = []

        def reason(code, label, points):
            reasons.append({"code": code, "label": label, "points": points})

        for genre in movie["genres"]:
            if genre.casefold() in preferred:
                reason("preferred_genre", "Genre choisi : " + genre, 5)
            if genres[genre.casefold()]:
                reason(
                    "similar_genre",
                    "Genre de vos films de référence : " + genre,
                    min(5, genres[genre.casefold()]),
                )
        if movie["director"].casefold() in directors:
            reason("similar_director", "Même réalisateur que l’un de vos films de référence", 2)
        common = len({actor.casefold() for actor in movie["actors"]} & actors)
        if common:
            reason(
                "similar_actors",
                "Interprètes en commun avec vos films de référence",
                min(2, common),
            )
        if movie["id"] in watchlist:
            reason("watchlist", "Présent dans votre liste à voir", 2)
        score = sum(item["points"] for item in reasons)
        if not reasons:
            reason(
                "catalogue", "Découverte du catalogue, sans similarité personnelle identifiée", 0
            )
        return {
            "movie": movie,
            "score": score,
            "reasons": reasons,
        }

    ranked = nsmallest(
        limit,
        (rank(movie) for identity, movie in movies.items() if identity not in excluded),
        key=lambda entry: (
            -entry["score"],
            entry["movie"]["title"].casefold(),
            entry["movie"]["id"],
        ),
    )
    return {
        "recommendations": ranked,
        "total": len(movies) - len(excluded.intersection(movies)),
        "limit": limit,
        "personalized": bool(seeds or preferred or watchlist),
        "method": "content-rules-v1",
    }


def with_availability(result, stock):
    return {
        **result,
        "recommendations": [
            {
                **entry,
                "movie": resource(entry["movie"]),
                "available_copies": stock.available(entry["movie"]["id"]),
            }
            for entry in result["recommendations"]
        ],
    }
