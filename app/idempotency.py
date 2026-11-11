import uuid

from flask import jsonify, make_response, request, url_for

from .enterprise_auth import error, now
from .receipt_storage import digest
from .services.receipts import RECEIPT_CAPACITY, RECEIPT_TTL, cached_problem, key_digest


def create_copy_once(document, user, data, create):
    return run_once(document, user, data, create, "copy.create", 201, required=False)


def run_once(document, user, data, create, operation, status, required=True):
    supplied = request.headers.get("Idempotency-Key")
    if supplied is None:
        if required:
            return error(428, "operation_key_required", "Une clé d’opération est requise.")
        return create()
    key_hash = key_digest(supplied)
    timestamp = now()
    request_hash = digest(data)
    records = document["idempotency_records"]
    records[:] = [r for r in records if r["expires_at"] > timestamp]
    cached = next(
        (r for r in records if r["actor_id"] == user["id"] and r["key_hash"] == key_hash), None
    )
    if cached:
        problem = cached_problem(cached, operation, request_hash, timestamp)
        if problem is not None:
            return error(problem.status, problem.error["code"], problem.error["message"])
        response = jsonify(cached["response"])
        response.status_code = status
        if operation == "copy.create":
            response.headers["Location"] = url_for(
                "enterprise_catalogue.get_copy", copy_id=cached["response"]["id"]
            )
            response.headers["ETag"] = cached["response"]["_etag"]
        response.headers["Idempotency-Replayed"] = "true"
        return response
    if len(records) >= RECEIPT_CAPACITY:
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
                "expires_at": timestamp + RECEIPT_TTL,
                "response": response.get_json(),
            }
        )
    return response
