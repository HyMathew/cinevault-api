"""Stdlib remote entry point: execute an exported controller corpus, not Hypothesis.

Import with tests/ on sys.path: from properties.native import exercise_corpus.
Database must be disposable, application-privileged, provisioned from the exact
current schema. Cleanup belongs to its owning isolated runner, never stable data.
"""

import datetime
import uuid

from bson import Int64
from pymongo.errors import OperationFailure

from app.persistence.mongo import MongoPersistence
from app.persistence.mongo_model import encode_record

from .harness import run_copy_actions, run_private_actions, scenario_fixture


def validate_corpus(corpus):
    if type(corpus) is not dict or corpus.get("format") != 1:
        raise ValueError("Invalid property corpus")
    cases = corpus.get("cases")
    if type(cases) is not list or len(cases) != 8:
        raise ValueError("Property corpus size is not bounded")
    domains = []
    for case in cases:
        if (
            type(case) is not dict
            or set(case) != {"domain", "actions"}
            or case["domain"] not in ("private", "receipts")
        ):
            raise ValueError("Invalid corpus case")
        domains.append(case["domain"])
        actions = case["actions"]
        if type(actions) is not list or len(actions) != 12:
            raise ValueError("Invalid corpus sequence length")
        for action in actions:
            if (
                type(action) not in (tuple, list)
                or len(action) != 4
                or any(type(value) is not int for value in action[:3])
                or not 0 <= action[0] <= 7
                or not 0 <= action[1] <= 2
                or not 0 <= action[2] <= 9
                or type(action[3]) is not str
                or not 1 <= len(action[3]) <= 80
                or action[3] != action[3].strip()
                or any(ord(c) < 32 for c in action[3])
            ):
                raise ValueError("Invalid corpus action")
    if domains.count("private") != 4 or domains.count("receipts") != 4:
        raise ValueError("Corpus must contain four sequences per domain")
    return cases


def run_corpus(persistence, corpus):
    cases = validate_corpus(corpus)  # Validate the entire bounded corpus before I/O.
    counts = {"private_steps": 0, "receipt_steps": 0, "sequences_completed": 0}
    for case in cases:
        with scenario_fixture(persistence) as fixture:
            operation = run_private_actions if case["domain"] == "private" else run_copy_actions
            steps = operation(fixture, case["actions"])
            counts["private_steps" if case["domain"] == "private" else "receipt_steps"] += steps
            counts["sequences_completed"] += 1
    return counts


def exercise_corpus(database, corpus):
    counts = run_corpus(MongoPersistence(database), corpus)
    users = database["users"]
    positive_id = str(uuid.uuid4())
    valid = encode_record(
        "users",
        {
            "id": positive_id,
            "username": "pbt_" + uuid.uuid4().hex[:20],
            "display_name": "Synthétique",
            "password_hash": "$argon2id$synthetic-property-fixture",
            "role": "member",
            "enabled": True,
            "created_at": 0,
        },
    )
    assert type(valid["created_at"]) is Int64
    inserted = False
    try:
        result = users.insert_one(valid)
        inserted = True
        assert result.acknowledged is True, "Positive control insert was not acknowledged"
        stored = users.find_one({"_id": positive_id})
        assert type(stored) is dict and set(stored) == set(valid), (
            "Positive control readback fields"
        )
        for key, value in valid.items():
            assert type(stored[key]) is type(value) and stored[key] == value, (
                "Positive control readback value/type: " + key
            )
    finally:
        deleted = users.delete_one({"_id": positive_id})
        if inserted:
            assert deleted.acknowledged is True and deleted.deleted_count == 1, (
                "Positive control UUID cleanup failed"
            )
    # Deliberately bypass the codec for illegal field types; this exercises the
    # native field validator. Every other field matches the accepted control.
    rejected = 0
    for value in (True, 0.5, datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)):
        identity = str(uuid.uuid4())
        encoded = {**valid, "_id": identity, "created_at": value}
        try:
            try:
                users.insert_one(encoded)
            except OperationFailure as error:
                assert error.code == 121, (
                    "Expected native field validation, not auth/driver failure"
                )
                rejected += 1
            else:
                raise AssertionError("Native validator accepted illegal integer-field type")
        finally:
            users.delete_one({"_id": identity})
    return {
        **counts,
        "status": "passed",
        "engine": "MongoDB",
        "generator_executed_here": False,
        "native_failure_shrinking": False,
        "oracle": "shared-stdlib-symbolic-model",
        "positive_control_native_valid_insert": True,
        "native_invalid_integer_field_types_rejected_121": rejected,
    }
