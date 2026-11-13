"""Loan and reservation policy shared by JSON and MongoDB repository ports."""

import copy
import uuid

from ..circulation_model import (
    MANAGERS,
    ForbiddenScope,
    derived_event_id,
    empty_body,
    list_query,
    payload,
    priority_allowed,
)
from ..personal_model import make_event
from ..receipt_storage import digest
from ..validation import ValidationError
from .authentication import authenticated, public_profile
from .catalogue import canonical_identity, missing, version_problem
from .catalogue_model import etag, resource
from .receipts import RECEIPT_CAPACITY, RECEIPT_TTL, cached_problem, key_digest
from .results import ServiceResult, failure


def expire_waiting(work, timestamp, namespace):
    for batch in work.circulation.expired_batches(timestamp):
        for entry in batch:
            entry.update(status="expired", closed_at=timestamp)
            work.circulation.save_reservation(entry)
            work.events.append(
                make_event(
                    derived_event_id(namespace, "reservation.expired", entry["id"]),
                    None,
                    "reservation",
                    entry["id"],
                    "expired",
                    timestamp,
                )
            )


def cancel_disabled_in_work(work, user_id, actor_id, timestamp, namespace):
    expire_waiting(work, timestamp, namespace)
    for batch in work.circulation.waiting_user_batches(user_id):
        for entry in batch:
            entry.update(status="cancelled", closed_at=timestamp)
            work.circulation.save_reservation(entry)
            work.events.append(
                make_event(
                    derived_event_id(namespace, "reservation.cancelled", entry["id"]),
                    actor_id,
                    "reservation",
                    entry["id"],
                    "cancelled",
                    timestamp,
                )
            )


def represented(entity, status=200):
    return ServiceResult(status, body=resource(entity), headers={"ETag": etag(entity)})


def forbidden():
    return failure(403, "forbidden", "Accès interdit.")


class CirculationService:
    def __init__(self, persistence):
        self._persistence = persistence

    def _authenticated(self, token, timestamp, operation):
        return authenticated(
            self._persistence, token, timestamp, lambda work, user, session: operation(work, user)
        )

    @staticmethod
    def _visible(work, user, identity, kind):
        if not canonical_identity(identity):
            return None
        entity = getattr(work.circulation, kind)(
            identity,
            owner_id=None if user["role"] in MANAGERS else user["id"],
        )
        owner = "borrower_id" if kind == "loan" else "user_id"
        return (
            entity if entity and (entity[owner] == user["id"] or user["role"] in MANAGERS) else None
        )

    @staticmethod
    def _borrower_problem(work, loan, timestamp):
        borrower = work.accounts.get(loan["borrower_id"])
        if borrower is None or not borrower["enabled"]:
            return failure(
                409, "borrower_disabled", "Ce compte ne peut pas recevoir un nouveau prêt."
            )
        if timestamp < loan["requested_at"]:
            return failure(409, "clock_state", "Horloge incohérente avec la demande.")
        if work.circulation.count_loans(loan["borrower_id"], "active") >= 5:
            return failure(409, "loan_limit", "Limite de prêts actifs atteinte.")
        return None

    @staticmethod
    def _priority(work, loan, timestamp):
        queue = work.circulation.queue_facts(
            loan["movie_id"], timestamp, user_id=loan["borrower_id"]
        )
        free = work.circulation.inventory_counts(loan["movie_id"])["free"]
        head = queue["head"]
        allowed = priority_allowed(
            owned=queue["owned"] is not None,
            is_head=head is not None and head["user_id"] == loan["borrower_id"],
            free=free,
            reserved=queue["total"],
        )
        return allowed, head

    @staticmethod
    def _receipt(work, user, data, operation, key, timestamp):
        key_hash = key_digest(key)
        if key_hash is None:
            return None, None, None
        request_hash = digest(data)
        work.receipts.prune_expired(timestamp)
        cached = work.receipts.get(user["id"], key_hash)
        if cached is not None:
            problem = cached_problem(cached, operation, request_hash, timestamp)
            if problem is not None:
                return key_hash, request_hash, problem
            return (
                key_hash,
                request_hash,
                ServiceResult(
                    201,
                    body=cached["response"],
                    headers={
                        "ETag": cached["response"]["_etag"],
                        "Idempotency-Replayed": "true",
                    },
                ),
            )
        if work.receipts.count() >= RECEIPT_CAPACITY:
            return (
                key_hash,
                request_hash,
                failure(503, "receipt_capacity", "Capacité d’opérations atteinte."),
            )
        return key_hash, request_hash, None

    @staticmethod
    def _save_receipt(work, user, body, operation, key_hash, request_hash, receipt_id, timestamp):
        if key_hash is not None:
            work.receipts.insert(
                {
                    "id": receipt_id,
                    "actor_id": user["id"],
                    "key_hash": key_hash,
                    "request_hash": request_hash,
                    "operation": operation,
                    "created_at": timestamp,
                    "expires_at": timestamp + RECEIPT_TTL,
                    "response": body,
                }
            )

    def request_loan(self, token, data, *, key=None, timestamp):
        data = copy.deepcopy(data)
        identity, event_id, receipt_id = (str(uuid.uuid4()) for _ in range(3))

        def create(work, user):
            supplied = payload(data, ("movie_id",))
            movie_id = supplied["movie_id"]
            if not isinstance(movie_id, str):
                raise ValidationError({"movie_id": "Identifiant de film invalide."})
            if not canonical_identity(movie_id):
                return missing()
            key_hash, request_hash, cached = self._receipt(
                work, user, supplied, "loan.request", key, timestamp
            )
            if cached is not None:
                return cached
            if work.catalogue.movie(movie_id) is None:
                return missing()
            if work.circulation.open_loan(user["id"], movie_id) is not None:
                return failure(409, "open_loan", "Une demande ou un prêt existe déjà pour ce film.")
            if work.circulation.count_loans(user["id"], "pending") >= 10:
                return failure(409, "loan_limit", "Limite de demandes atteinte.")
            loan = {
                "id": identity,
                "movie_id": movie_id,
                "borrower_id": user["id"],
                "copy_id": None,
                "status": "pending",
                "requested_at": timestamp,
                "approved_at": None,
                "due_at": None,
                "returned_at": None,
                "cancelled_at": None,
            }
            work.circulation.insert_loan(loan)
            work.events.append(
                make_event(event_id, user["id"], "loan", identity, "requested", timestamp)
            )
            result = represented(loan, 201)
            self._save_receipt(
                work,
                user,
                result.body,
                "loan.request",
                key_hash,
                request_hash,
                receipt_id,
                timestamp,
            )
            return result

        return self._authenticated(token, timestamp, create)

    def loan(self, token, identity, *, timestamp):
        def read(work, user):
            entity = self._visible(work, user, identity, "loan")
            return missing() if entity is None else represented(entity)

        return self._authenticated(token, timestamp, read)

    def loans(self, token, parameters, *, timestamp):
        parameters = copy.deepcopy(parameters)

        def read(work, user):
            try:
                query = list_query(parameters, "loan", user["role"])
            except ForbiddenScope:
                return forbidden()
            rows, total = work.circulation.loans_page(
                user["id"] if query.scope == "mine" else None, query
            )
            movies = work.catalogue.movies_by_ids(list({row["movie_id"] for row in rows}))
            users = work.accounts.by_ids(list({row["borrower_id"] for row in rows}))
            return ServiceResult(
                200,
                body={
                    "loans": [resource(row) for row in rows],
                    "total": total,
                    "limit": query.limit,
                    "offset": query.offset,
                    "movies": {movie["id"]: resource(movie) for movie in movies},
                    "borrowers": {account["id"]: public_profile(account) for account in users},
                    "overdue_ids": [
                        row["id"]
                        for row in rows
                        if row["status"] == "active" and row["due_at"] < timestamp
                    ],
                },
            )

        return self._authenticated(token, timestamp, read)

    def approve(self, token, identity, data, *, if_match, timestamp):
        data = copy.deepcopy(data)
        namespace = uuid.uuid4()
        approved_id, fulfilled_id = str(uuid.uuid4()), str(uuid.uuid4())

        def approve(work, user):
            if user["role"] not in MANAGERS:
                return forbidden()
            expire_waiting(work, timestamp, namespace)
            loan = work.circulation.loan(identity) if canonical_identity(identity) else None
            if loan is None:
                return missing()
            problem = version_problem(loan, if_match)
            if problem is not None:
                return problem
            if loan["status"] != "pending":
                return failure(409, "loan_state", "Seule une demande en attente peut être validée.")
            problem = self._borrower_problem(work, loan, timestamp)
            if problem is not None:
                return problem
            supplied = payload(data, ("copy_id", "due_at"))
            if (
                not isinstance(supplied["copy_id"], str)
                or type(supplied["due_at"]) is not int
                or not timestamp < supplied["due_at"] <= timestamp + 90 * 86400
            ):
                raise ValidationError({"body": "Exemplaire ou échéance invalide."})
            item = (
                work.catalogue.copy(supplied["copy_id"])
                if canonical_identity(supplied["copy_id"])
                else None
            )
            if item is None:
                return missing()
            if (
                item["movie_id"] != loan["movie_id"]
                or item["condition"] == "damaged"
                or work.circulation.active_for_copy(item["id"])
            ):
                return failure(409, "copy_unavailable", "Cet exemplaire ne peut pas être attribué.")
            allowed, head = self._priority(work, loan, timestamp)
            if not allowed:
                return failure(
                    409,
                    "reservation_priority",
                    "Une réservation prioritaire doit être traitée avant cette demande.",
                )
            loan.update(
                copy_id=item["id"],
                status="active",
                approved_at=timestamp,
                due_at=supplied["due_at"],
            )
            work.circulation.save_loan(loan)
            if head is not None and head["user_id"] == loan["borrower_id"]:
                head.update(status="fulfilled", closed_at=timestamp, loan_id=identity)
                work.circulation.save_reservation(head)
                work.events.append(
                    make_event(
                        fulfilled_id, user["id"], "reservation", head["id"], "fulfilled", timestamp
                    )
                )
            work.events.append(
                make_event(approved_id, user["id"], "loan", identity, "approved", timestamp)
            )
            return represented(loan)

        return self._authenticated(token, timestamp, approve)

    def options(self, token, identity, *, timestamp):
        namespace = uuid.uuid4()

        def read(work, user):
            if user["role"] not in MANAGERS:
                return forbidden()
            expire_waiting(work, timestamp, namespace)
            loan = work.circulation.loan(identity) if canonical_identity(identity) else None
            if loan is None:
                return missing()
            if loan["status"] != "pending":
                return failure(409, "loan_state", "Cette demande n’est plus en attente.")
            problem = self._borrower_problem(work, loan, timestamp)
            if problem is not None:
                return problem
            allowed, _ = self._priority(work, loan, timestamp)
            rows = work.circulation.free_copies(loan["movie_id"]) if allowed else []
            return ServiceResult(
                200, body={"loan_id": identity, "copies": [resource(row) for row in rows]}
            )

        return self._authenticated(token, timestamp, read)

    def return_loan(self, token, identity, *, if_match, timestamp, body=b""):
        event_id = str(uuid.uuid4())

        def returned(work, user):
            empty_body(body)
            if user["role"] not in MANAGERS:
                return forbidden()
            loan = work.circulation.loan(identity) if canonical_identity(identity) else None
            if loan is None:
                return missing()
            problem = version_problem(loan, if_match)
            if problem is not None:
                return problem
            if loan["status"] != "active" or timestamp < loan["approved_at"]:
                return failure(409, "loan_state", "Ce prêt ne peut pas être retourné.")
            loan.update(status="returned", returned_at=timestamp)
            work.circulation.save_loan(loan)
            work.events.append(
                make_event(event_id, user["id"], "loan", identity, "returned", timestamp)
            )
            return represented(loan)

        return self._authenticated(token, timestamp, returned)

    def cancel_loan(self, token, identity, *, if_match, timestamp, body=b""):
        event_id = str(uuid.uuid4())

        def cancel(work, user):
            empty_body(body)
            loan = self._visible(work, user, identity, "loan")
            if loan is None:
                return missing()
            problem = version_problem(loan, if_match)
            if problem is not None:
                return problem
            if loan["status"] != "pending" or timestamp < loan["requested_at"]:
                return failure(409, "loan_state", "Seule une demande en attente peut être annulée.")
            loan.update(status="cancelled", cancelled_at=timestamp)
            work.circulation.save_loan(
                loan, owner_id=None if user["role"] in MANAGERS else user["id"]
            )
            work.events.append(
                make_event(event_id, user["id"], "loan", identity, "cancelled", timestamp)
            )
            return represented(loan)

        return self._authenticated(token, timestamp, cancel)

    @staticmethod
    def _availability(work, movie_id, timestamp):
        counts = work.circulation.inventory_counts(movie_id)
        reserved = work.circulation.queue_facts(movie_id, timestamp)["total"]
        return {
            "movie_id": movie_id,
            **counts,
            "reserved": reserved,
            "available": max(0, counts["free"] - reserved),
        }

    def availability(self, parameters, *, timestamp):
        if (
            len(parameters) != 1
            or parameters[0][0] != "movie_id"
            or type(parameters[0][1]) is not str
        ):
            raise ValidationError({"movie_id": "Film requis."})
        identity = parameters[0][1]

        def read(work):
            if not canonical_identity(identity) or work.catalogue.movie(identity) is None:
                return missing()
            return ServiceResult(200, body=self._availability(work, identity, timestamp))

        return self._persistence.query(read)

    def create_reservation(self, token, data, *, key=None, timestamp):
        data = copy.deepcopy(data)
        namespace = uuid.uuid4()
        identity, event_id, receipt_id = (str(uuid.uuid4()) for _ in range(3))

        def create(work, user):
            expire_waiting(work, timestamp, namespace)
            supplied = payload(data, ("movie_id",))
            movie_id = supplied["movie_id"]
            if not isinstance(movie_id, str):
                raise ValidationError({"movie_id": "Identifiant de film invalide."})
            if not canonical_identity(movie_id):
                return missing()
            key_hash, request_hash, cached = self._receipt(
                work, user, supplied, "reservation.create", key, timestamp
            )
            if cached is not None:
                return cached
            if work.catalogue.movie(movie_id) is None:
                return missing()
            if (
                work.circulation.queue_facts(movie_id, timestamp, user_id=user["id"])["owned"]
                is not None
            ):
                return failure(409, "duplicate", "Vous attendez déjà ce film.")
            if work.circulation.count_waiting_for_user(user["id"]) >= 10:
                return failure(
                    409, "reservation_limit", "Limite de réservations en attente atteinte."
                )
            open_loan = work.circulation.open_loan(user["id"], movie_id)
            if open_loan is not None and open_loan["status"] == "active":
                return failure(409, "active_loan", "Vous avez déjà emprunté ce film.")
            if self._availability(work, movie_id, timestamp)["available"] > 0:
                return failure(
                    409, "copy_available", "Un exemplaire est disponible : demandez un prêt."
                )
            entry = {
                "id": identity,
                "movie_id": movie_id,
                "user_id": user["id"],
                "status": "waiting",
                "sequence": work.revision,
                "created_at": timestamp,
                "expires_at": timestamp + 14 * 86400,
                "closed_at": None,
                "loan_id": None,
            }
            work.circulation.insert_reservation(entry)
            work.events.append(
                make_event(event_id, user["id"], "reservation", identity, "created", timestamp)
            )
            result = represented(entry, 201)
            self._save_receipt(
                work,
                user,
                result.body,
                "reservation.create",
                key_hash,
                request_hash,
                receipt_id,
                timestamp,
            )
            return result

        return self._authenticated(token, timestamp, create)

    def reservation(self, token, identity, *, timestamp):
        namespace = uuid.uuid4()

        def read(work, user):
            expire_waiting(work, timestamp, namespace)
            entry = self._visible(work, user, identity, "reservation")
            return missing() if entry is None else represented(entry)

        return self._authenticated(token, timestamp, read)

    def cancel_reservation(self, token, identity, *, if_match, timestamp, body=b""):
        namespace, event_id = uuid.uuid4(), str(uuid.uuid4())

        def cancel(work, user):
            expire_waiting(work, timestamp, namespace)
            empty_body(body)
            entry = self._visible(work, user, identity, "reservation")
            if entry is None:
                return missing()
            problem = version_problem(entry, if_match)
            if problem is not None:
                return problem
            if entry["status"] != "waiting" or timestamp < entry["created_at"]:
                return failure(
                    409, "reservation_state", "Cette réservation ne peut pas être annulée."
                )
            entry.update(status="cancelled", closed_at=timestamp)
            work.circulation.save_reservation(
                entry, owner_id=None if user["role"] in MANAGERS else user["id"]
            )
            work.events.append(
                make_event(event_id, user["id"], "reservation", identity, "cancelled", timestamp)
            )
            return represented(entry)

        return self._authenticated(token, timestamp, cancel)

    def reservations(self, token, parameters, *, timestamp):
        parameters = copy.deepcopy(parameters)
        namespace = uuid.uuid4()

        def read(work, user):
            expire_waiting(work, timestamp, namespace)
            try:
                query = list_query(parameters, "reservation", user["role"])
            except ForbiddenScope:
                return forbidden()
            rows, total = work.circulation.reservations_page(
                user["id"] if query.scope == "mine" else None, query
            )
            movies = work.catalogue.movies_by_ids(list({row["movie_id"] for row in rows}))
            users = work.accounts.by_ids(list({row["user_id"] for row in rows}))
            positions, ready = {}, []
            for row in rows:
                if row["status"] != "waiting" or row["expires_at"] <= timestamp:
                    continue
                queue = work.circulation.queue_facts(
                    row["movie_id"], timestamp, before_sequence=row["sequence"]
                )
                positions[row["id"]] = queue["preceding"] + 1
                if (
                    queue["head"]["id"] == row["id"]
                    and work.circulation.inventory_counts(row["movie_id"])["free"]
                ):
                    ready.append(row["id"])
            pairs = list({(row["user_id"], row["movie_id"]) for row in rows})
            opened = work.circulation.open_loans_for_pairs(pairs)
            opened = {(row["borrower_id"], row["movie_id"]): row["id"] for row in opened}
            return ServiceResult(
                200,
                body={
                    "reservations": [resource(row) for row in rows],
                    "total": total,
                    "limit": query.limit,
                    "offset": query.offset,
                    "movies": {movie["id"]: resource(movie) for movie in movies},
                    "users": {account["id"]: public_profile(account) for account in users},
                    "positions": positions,
                    "ready_ids": ready,
                    "open_loan_ids": {
                        row["id"]: opened[(row["user_id"], row["movie_id"])]
                        for row in rows
                        if (row["user_id"], row["movie_id"]) in opened
                    },
                },
            )

        return self._authenticated(token, timestamp, read)
