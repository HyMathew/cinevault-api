"""Entirely synthetic related entities for persistence contract tests."""

import uuid

from app.receipt_storage import digest
from app.storage_v2 import empty_document, validate_document


def related_document():
    ids = {
        name: str(uuid.UUID(int=index))
        for index, name in enumerate(
            (
                "user",
                "session",
                "movie",
                "copy",
                "entry",
                "loan",
                "reservation",
                "event",
                "receipt",
            ),
            start=1,
        )
    }
    doc = empty_document()
    doc["revision"] = 1
    doc["users"] = [
        {
            "id": ids["user"],
            "username": "synthetic-admin",
            "display_name": "Compte fictif",
            "password_hash": "$argon2id$synthetic-not-a-login-secret",
            "role": "admin",
            "enabled": True,
            "created_at": 0,
        }
    ]
    doc["sessions"] = [
        {
            "id": ids["session"],
            "user_id": ids["user"],
            "token_hash": "a" * 64,
            "created_at": 1,
            "last_seen": 1,
            "expires_at": 100,
        }
    ]
    doc["auth_attempts"] = [{"key": "b" * 64, "window_start": 1, "count": 1}]
    doc["movies"] = [
        {
            "id": ids["movie"],
            "title": "Échos fictifs",
            "year": 2026,
            "director": "Créateur fictif",
            "actors": ["Nom, prénom", "CR\rLF\nNUL\0🎬"],
            "synopsis": "",
            "genres": ["Fictif"],
            "external_source": {"qid": "Q1", "revision": 10**15, "retrieved_at": 2**53},
        }
    ]
    doc["copies"] = [
        {
            "id": ids["copy"],
            "movie_id": ids["movie"],
            "support": "dvd",
            "location": "",
            "condition": "good",
        }
    ]
    doc["collections"] = [
        {
            "id": ids["entry"],
            "owner_id": ids["user"],
            "movie_id": ids["movie"],
            "kind": "custom",
            "list_name": "Straße Σς",
            "supports": [],
            "created_at": 1,
        }
    ]
    doc["loans"] = [
        {
            "id": ids["loan"],
            "movie_id": ids["movie"],
            "borrower_id": ids["user"],
            "copy_id": ids["copy"],
            "status": "active",
            "requested_at": 1,
            "approved_at": 2,
            "due_at": 86402,
            "returned_at": None,
            "cancelled_at": None,
        }
    ]
    doc["reservations"] = [
        {
            "id": ids["reservation"],
            "movie_id": ids["movie"],
            "user_id": ids["user"],
            "status": "fulfilled",
            "sequence": 1,
            "created_at": 1,
            "expires_at": 86401,
            "closed_at": 2,
            "loan_id": ids["loan"],
        }
    ]
    doc["audit_events"] = [
        {
            "id": ids["event"],
            "actor_id": ids["user"],
            "action": "loan.approved",
            "entity_type": "loan",
            "entity_id": ids["loan"],
            "timestamp": 2,
        }
    ]
    response = doc["copies"][0].copy()
    response["_etag"] = '"' + digest(response) + '"'
    doc["idempotency_records"] = [
        {
            "id": ids["receipt"],
            "actor_id": ids["user"],
            "key_hash": "c" * 64,
            "request_hash": digest(
                {key: value for key, value in doc["copies"][0].items() if key != "id"}
            ),
            "operation": "copy.create",
            "created_at": 1,
            "expires_at": 101,
            "response": response,
        }
    ]
    return validate_document(doc)
