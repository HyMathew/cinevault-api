"""Only explicitly allowed, non-authentication creation receipts are persisted."""

import hashlib
import json
import re

from .validation import SUPPORTS, is_unicode

HASH = re.compile(r"[0-9a-f]{64}")


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _validate_circulation_response(record, valid_uuid):
    response = record["response"]
    loan = record["operation"] == "loan.request"
    fields = (
        {
            "id",
            "movie_id",
            "borrower_id",
            "copy_id",
            "status",
            "requested_at",
            "approved_at",
            "due_at",
            "returned_at",
            "cancelled_at",
        }
        if loan
        else {
            "id",
            "movie_id",
            "user_id",
            "status",
            "sequence",
            "created_at",
            "expires_at",
            "closed_at",
            "loan_id",
        }
    )
    if type(response) is not dict or set(response) != {*fields, "_etag"}:
        raise ValueError("Invalid cached circulation response")
    actor = "borrower_id" if loan else "user_id"
    created = "requested_at" if loan else "created_at"
    if (
        not valid_uuid(response["id"])
        or not valid_uuid(response["movie_id"])
        or response[actor] != record["actor_id"]
        or type(response[created]) is not int
        or response[created] != record["created_at"]
        or response["status"] != ("pending" if loan else "waiting")
    ):
        raise ValueError("Invalid cached circulation identity or state")
    nulls = (
        ("copy_id", "approved_at", "due_at", "returned_at", "cancelled_at")
        if loan
        else ("closed_at", "loan_id")
    )
    if any(response[field] is not None for field in nulls):
        raise ValueError("Invalid cached circulation dates")
    if not loan and (
        type(response["sequence"]) is not int
        or response["sequence"] < 1
        or type(response["expires_at"]) is not int
        or response["expires_at"] != record["created_at"] + 14 * 86400
    ):
        raise ValueError("Invalid cached reservation sequence or expiration")
    body = {key: value for key, value in response.items() if key != "_etag"}
    if (
        response["_etag"] != '"' + digest(body) + '"'
        or digest({"movie_id": response["movie_id"]}) != record["request_hash"]
    ):
        raise ValueError("Inconsistent cached circulation receipt")


def validate_receipts(document, ids, valid_uuid):
    records = document["idempotency_records"]
    if len(records) > 10000:
        raise ValueError("Receipt capacity exceeded")
    users = {u["id"] for u in document["users"]}
    keys = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "id",
            "actor_id",
            "key_hash",
            "request_hash",
            "operation",
            "created_at",
            "expires_at",
            "response",
        }:
            raise ValueError("Invalid receipt fields")
        if (
            not valid_uuid(record["id"])
            or record["id"] in ids
            or record["actor_id"] not in users
            or record["operation"]
            not in ("copy.create", "catalog.import", "loan.request", "reservation.create")
        ):
            raise ValueError("Invalid receipt identity")
        if any(
            not isinstance(record[k], str) or not HASH.fullmatch(record[k])
            for k in ("key_hash", "request_hash")
        ):
            raise ValueError("Invalid receipt digest")
        if (
            any(type(record[k]) is not int for k in ("created_at", "expires_at"))
            or not 0 <= record["created_at"] < record["expires_at"] <= record["created_at"] + 86400
        ):
            raise ValueError("Invalid receipt timestamps")
        key = (record["actor_id"], record["key_hash"])
        if key in keys:
            raise ValueError("Duplicate receipt key")
        response = record["response"]
        if record["operation"] in ("loan.request", "reservation.create"):
            _validate_circulation_response(record, valid_uuid)
            ids.add(record["id"])
            keys.add(key)
            continue
        if record["operation"] == "catalog.import":
            counts = ("created_movies", "created_copies", "skipped_movies", "skipped_copies")
            if (
                not isinstance(response, dict)
                or set(response) != {"operation_id", *counts}
                or not valid_uuid(response["operation_id"])
            ):
                raise ValueError("Invalid import receipt response")
            if any(
                type(response[k]) is not int
                or not 0 <= response[k] <= (1000 if k.endswith("movies") else 5000)
                for k in counts
            ):
                raise ValueError("Invalid import receipt counts")
            ids.add(record["id"])
            keys.add(key)
            continue
        if not isinstance(response, dict) or set(response) != {
            "id",
            "movie_id",
            "support",
            "location",
            "condition",
            "_etag",
        }:
            raise ValueError("Invalid cached response")
        if (
            not valid_uuid(response["id"])
            or not valid_uuid(response["movie_id"])
            or response["support"] not in SUPPORTS
            or response["condition"] not in ("unknown", "good", "damaged")
        ):
            raise ValueError("Invalid cached copy")
        if (
            not isinstance(response["location"], str)
            or not is_unicode(response["location"])
            or len(response["location"]) > 200
        ):
            raise ValueError("Invalid cached location")
        body = {k: v for k, v in response.items() if k != "_etag"}
        request_body = {k: v for k, v in body.items() if k != "id"}
        if (
            response["_etag"] != '"' + digest(body) + '"'
            or digest(request_body) != record["request_hash"]
        ):
            raise ValueError("Inconsistent cached receipt")
        ids.add(record["id"])
        keys.add(key)
