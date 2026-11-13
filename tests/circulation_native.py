"""Additional checks that require an owned, provisioned real MongoDB database."""

from bson import Int64
from pymongo.errors import OperationFailure
from test_circulation_integrity import exercise_expiration_batches
from test_circulation_service import NOW, implementation, lending_fixture

from app.persistence.errors import CommitOutcomeUnknown
from app.persistence.mongo import MongoPersistence
from app.persistence.mongo_model import encode_record
from app.persistence.mongo_transactions import TransactionRunner


def exercise_native_expiration_batches(database):
    backend = MongoPersistence(database)

    def prepare(users, movies, entries, revision):
        # Fixture setup on a disposable DB, not an application persistence path.
        for name, rows in (("users", users), ("movies", movies), ("reservations", entries)):
            database[name].insert_many([encode_record(name, row) for row in rows])
            database.entity_ids.insert_many([{"_id": row["id"], "kind": name} for row in rows])
        changed = database.coordination.update_one(
            {"_id": "business", "revision": {"$lt": Int64(revision)}},
            {"$set": {"revision": Int64(revision)}},
        )
        assert changed.modified_count == 1

    return exercise_expiration_batches(backend, prepare)


def exercise_native_commit_uncertainty(database):
    backend = MongoPersistence(database)
    actors, movie, _ = lending_fixture(backend)
    commits, bodies = [], []

    class LostCommitAcknowledgments:
        def start_session(self):
            session = database.client.start_session()
            original = session.commit_transaction

            def commit():
                original()  # Real server commit succeeds before its reply is hidden.
                commits.append(True)
                raise OperationFailure(
                    "injected commit acknowledgment loss",
                    code=91,
                    details={"errorLabels": ["UnknownTransactionCommitResult"]},
                )

            session.commit_transaction = commit
            return session

    uncertain = MongoPersistence(database)
    uncertain._runner = TransactionRunner(LostCommitAcknowledgments(), max_commit_attempts=2)
    original_run = uncertain._runner.run

    def observed(command):
        def body(session):
            bodies.append(True)
            return command(session)

        return original_run(body)

    uncertain._runner.run = observed
    token, key = actors["owner"][1], "native-uncertain-circulation-001"
    try:
        implementation()(uncertain).request_loan(
            token, {"movie_id": movie["id"]}, key=key, timestamp=NOW
        )
    except CommitOutcomeUnknown:
        pass
    else:
        raise AssertionError("Unknown native commit was reported as success")
    assert len(bodies) == 1 and len(commits) == 2
    replay = implementation()(backend).request_loan(
        token, {"movie_id": movie["id"]}, key=key, timestamp=NOW
    )
    assert replay.status == 201 and replay.headers["Idempotency-Replayed"] == "true"
    current = implementation()(backend).loans(token, [], timestamp=NOW).body
    assert current["total"] == 1 and current["loans"][0]["id"] == replay.body["id"]
    events, _ = backend.query(lambda work: work.events.page(100, 0))
    assert len([row for row in events if row["entity_id"] == replay.body["id"]]) == 1
    return {
        "real_native_commit_then_injected_ack_loss": "passed",
        "command_executions": len(bodies),
        "commit_executions": len(commits),
    }


def _plan_facts(result):
    names, stages = set(), set()

    def inspect(value):
        if isinstance(value, dict):
            if "indexName" in value:
                names.add(value["indexName"])
            if "stage" in value:
                stages.add(value["stage"])
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    inspect(result.get("queryPlanner", {}).get("winningPlan", {}))
    stats = result["executionStats"]
    inspect(stats.get("executionStages", {}))
    return {
        "indexes": sorted(names),
        "blocking_sort": "SORT" in stages,
        "returned": stats["nReturned"],
        "documents_examined": stats["totalDocsExamined"],
    }


def exercise_native_query_plans(database):
    loan = database.loans.find_one({}, {"borrower_id": 1})
    reservation = database.reservations.find_one({}, {"user_id": 1})
    assert loan is not None and reservation is not None
    reports = []
    for collection, selector, order, index in (
        ("loans", {}, {"requested_at": -1, "_id": -1}, "loan_time_order"),
        (
            "loans",
            {"borrower_id": loan["borrower_id"]},
            {"requested_at": -1, "_id": -1},
            "borrower_time_order",
        ),
        (
            "reservations",
            {"user_id": reservation["user_id"]},
            {"sequence": -1},
            "user_reservation_order",
        ),
    ):
        query = {"find": collection, "filter": selector, "sort": order, "limit": 24}
        chosen = _plan_facts(database.command({"explain": query, "verbosity": "executionStats"}))
        hinted = _plan_facts(
            database.command({"explain": {**query, "hint": index}, "verbosity": "executionStats"})
        )
        assert hinted["indexes"] == [index] and not hinted["blocking_sort"]
        assert 0 <= hinted["returned"] <= 24 and hinted["documents_examined"] <= 24
        reports.append(
            {
                "collection": collection,
                "candidate_index": index,
                "optimizer": chosen,
                "explicit_hint": hinted,
            }
        )
    return {"measured_query_plans": reports, "query_plan_fixture_is_not_a_load_benchmark": True}
