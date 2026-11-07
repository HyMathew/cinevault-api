"""Queue rules run only inside the caller's existing store transaction."""

from .audit import record_event


def waiting(document, movie_id, timestamp):
    return sorted(
        (
            r
            for r in document["reservations"]
            if r["movie_id"] == movie_id
            and r["status"] == "waiting"
            and r["expires_at"] > timestamp
        ),
        key=lambda r: r["sequence"],
    )


def free_copies(document, movie_id):
    occupied = {
        candidate_loan["copy_id"]
        for candidate_loan in document["loans"]
        if candidate_loan["status"] == "active"
    }
    return [
        c
        for c in document["copies"]
        if c["movie_id"] == movie_id and c["condition"] != "damaged" and c["id"] not in occupied
    ]


def availability(document, movie_id, timestamp):
    copies = [c for c in document["copies"] if c["movie_id"] == movie_id]
    active = {
        candidate_loan["copy_id"]
        for candidate_loan in document["loans"]
        if candidate_loan["movie_id"] == movie_id and candidate_loan["status"] == "active"
    }
    free = len(free_copies(document, movie_id))
    reserved = len(waiting(document, movie_id, timestamp))
    return {
        "movie_id": movie_id,
        "total": len(copies),
        "loaned": len(active),
        "free": free,
        "reserved": reserved,
        "available": max(0, free - reserved),
    }


def expire_waiting(document, timestamp):
    for reservation in document["reservations"]:
        if reservation["status"] == "waiting" and reservation["expires_at"] <= timestamp:
            reservation.update(status="expired", closed_at=timestamp)
            record_event(document, None, "reservation", reservation["id"], "expired")


def cancel_disabled(document, user_id, actor_id, timestamp):
    expire_waiting(document, timestamp)
    for reservation in document["reservations"]:
        if reservation["user_id"] == user_id and reservation["status"] == "waiting":
            reservation.update(status="cancelled", closed_at=timestamp)
            record_event(document, actor_id, "reservation", reservation["id"], "cancelled")


def priority_allows(document, loan, timestamp):
    queue = waiting(document, loan["movie_id"], timestamp)
    if any(r["user_id"] == loan["borrower_id"] for r in queue):
        return queue[0]["user_id"] == loan["borrower_id"]
    return len(free_copies(document, loan["movie_id"])) > len(queue)


def fulfill(document, loan, actor_id, timestamp):
    queue = waiting(document, loan["movie_id"], timestamp)
    if queue and queue[0]["user_id"] == loan["borrower_id"]:
        reservation = queue[0]
        reservation.update(status="fulfilled", closed_at=timestamp, loan_id=loan["id"])
        record_event(document, actor_id, "reservation", reservation["id"], "fulfilled")
