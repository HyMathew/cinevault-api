"""Deterministic circulation inputs and policy; no HTTP, clock or driver access."""

import copy
import json
import uuid
from dataclasses import dataclass

from .business_storage import AUDIT_ACTIONS
from .personal_model import page_values
from .repository import unique_pairs
from .validation import ValidationError, is_unicode

MANAGERS = ("manager", "admin")
LOAN_STATUSES = ("pending", "active", "returned", "cancelled")
RESERVATION_STATUSES = ("waiting", "fulfilled", "cancelled", "expired")


@dataclass(frozen=True)
class RequestBody:
    raw: bytes
    is_json: bool
    error: int | None = None


class PayloadError(Exception):
    def __init__(self, status):
        self.status = status


class ForbiddenScope(Exception):
    """A normal scope refusal, distinct from a validation exception."""


def payload(data, required):
    if isinstance(data, RequestBody):
        if not data.is_json:
            raise PayloadError(415)
        if data.error is not None:
            raise PayloadError(data.error)
        try:
            data = json.loads(
                data.raw,
                object_pairs_hook=unique_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise PayloadError(400) from None
    if type(data) is not dict or set(data) != set(required):
        raise ValidationError({"body": "Champs invalides."})
    return copy.deepcopy(data)


def empty_body(body):
    if isinstance(body, RequestBody):
        if body.error is not None:
            raise PayloadError(body.error)
        body = body.raw
    if body:
        raise ValidationError({"body": "Aucun corps attendu."})


@dataclass(frozen=True)
class CirculationQuery:
    status: str = ""
    scope: str = "mine"
    movie_id: str = ""
    limit: int = 24
    offset: int = 0

    def __post_init__(self):
        if (
            type(self.status) is not str
            or self.status not in ("", "open", *LOAN_STATUSES, *RESERVATION_STATUSES)
            or self.scope not in ("mine", "all")
            or type(self.movie_id) is not str
            or not is_unicode(self.movie_id)
            or type(self.limit) is not int
            or not 1 <= self.limit <= 100
            or type(self.offset) is not int
            or not 0 <= self.offset <= 10000
        ):
            raise ValueError("Invalid circulation query")


def list_query(parameters, kind, role):
    values = {}
    for name, value in parameters:
        if (
            name not in {"status", "scope", "movie_id", "limit", "offset"}
            or name in values
            or type(value) is not str
            or not is_unicode(value)
        ):
            raise ValidationError({"query": "Paramètres invalides."})
        values[name] = value
    status = values.get("status", "")
    statuses = ("", "open", *LOAN_STATUSES) if kind == "loan" else ("", *RESERVATION_STATUSES)
    if status not in statuses:
        raise ValidationError({"status": "Statut invalide."})
    scope = values.get("scope", "all" if role in MANAGERS else "mine")
    if scope not in ("mine", "all"):
        raise ValidationError({"scope": "Périmètre invalide."})
    if scope == "all" and role not in MANAGERS:
        raise ForbiddenScope()
    limit, offset = page_values(values, 24)
    return CirculationQuery(status, scope, values.get("movie_id", ""), limit, offset)


def derived_event_id(namespace, action, identity):
    if (
        type(namespace) is not uuid.UUID
        or action not in AUDIT_ACTIONS
        or action not in ("reservation.expired", "reservation.cancelled")
        or type(identity) is not str
        or str(uuid.UUID(identity)) != identity
    ):
        raise ValueError("Invalid derived circulation event identity")
    return str(uuid.uuid5(namespace, f"circulation-v1:{action}:{identity}"))


def priority_allowed(*, owned, is_head, free, reserved):
    return is_head if owned else free > reserved
