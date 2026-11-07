import hashlib
import re
import uuid

from flask import jsonify, make_response, request, url_for

from .enterprise_auth import error, now
from .receipt_storage import digest
from .validation import ValidationError

KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")


def create_copy_once(document, user, data, create):
    return run_once(document, user, data, create, "copy.create", 201, required=False)


def run_once(document, user, data, create, operation, status, required=True):
    supplied = request.headers.get("Idempotency-Key")
    if supplied is None:
        if required:
            return error(428, "operation_key_required", "Une clé d’opération est requise.")
        return create()
    if not KEY.fullmatch(supplied):
        raise ValidationError({"Idempotency-Key": "Clé d’opération invalide."})
    timestamp = now()
    key_hash = hashlib.sha256(supplied.encode("ascii")).hexdigest()
    request_hash = digest(data)
    records = document["idempotency_records"]
    records[:] = [r for r in records if r["expires_at"] > timestamp]
    cached = next(
        (r for r in records if r["actor_id"] == user["id"] and r["key_hash"] == key_hash), None
    )
    if cached:
        if cached["created_at"] > timestamp:
            return error(409, "clock_state", "Horloge incohérente avec cette opération.")
        if cached["operation"] != operation or cached["request_hash"] != request_hash:
            return error(409, "operation_conflict", "Cette clé a déjà servi avec d’autres données.")
        response = jsonify(cached["response"])
        response.status_code = status
        if operation == "copy.create":
            response.headers["Location"] = url_for(
                "enterprise_catalogue.get_copy", copy_id=cached["response"]["id"]
            )
            response.headers["ETag"] = cached["response"]["_etag"]
        response.headers["Idempotency-Replayed"] = "true"
        return response
    if len(records) >= 10000:
        return error(503, "receipt_capacity", "Capacité d’opérations atteinte.")
    response = make_response(create())
    if response.status_code == status:
        records.append(
            {
                "id": str(uuid.uuid4()),
                "actor_id": user["id"],
                "key_hash": key_hash,
                "request_hash": request_hash,
                "operation": operation,
                "created_at": timestamp,
                "expires_at": timestamp + 86400,
                "response": response.get_json(),
            }
        )
    return response
