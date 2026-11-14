"""Bounded portable P4 corpus; no Hypothesis, HTTP or shell dependency."""

from .circulation import (
    expired_receipt_scenario,
    fifo_scenario,
    maintenance_scenario,
    quota_scenario,
    receipt_scenario,
)


def validate_circulation_corpus(corpus):
    if type(corpus) is not dict or set(corpus) != {"format", "domain", "seed", "cases"}:
        raise ValueError("Invalid circulation corpus")
    if (
        type(corpus["format"]) is not int
        or corpus["format"] != 1
        or corpus["domain"] != "circulation"
        or type(corpus["seed"]) is not int
    ):
        raise ValueError("Invalid circulation corpus metadata")
    cases = corpus["cases"]
    if type(cases) is not list or not 1 <= len(cases) <= 8:
        raise ValueError("Invalid circulation corpus size")
    for case in cases:
        if type(case) is not dict or type(case.get("domain")) is not str:
            raise ValueError("Invalid circulation case")
        domain = case["domain"]
        if domain == "fifo":
            if set(case) != {"domain", "order", "cancelled", "expiry"}:
                raise ValueError("Invalid FIFO fields")
            order, cancelled = case["order"], case["cancelled"]
            if (
                type(order) is not list
                or len(order) != 3
                or any(type(a) is not int for a in order)
                or set(order) != {0, 1, 3}
                or type(cancelled) is not list
                or any(type(a) is not int for a in cancelled)
                or len(cancelled) != len(set(cancelled))
                or not set(cancelled) <= {0, 1, 3}
                or case["expiry"] not in ("none", "before", "at", "after")
            ):
                raise ValueError("Invalid FIFO plan")
        elif domain == "quotas":
            if set(case) != {"domain", "order", "returned_slot"}:
                raise ValueError("Invalid quota fields")
            order = case["order"]
            if (
                type(order) is not list
                or len(order) != 11
                or any(type(a) is not int for a in order)
                or set(order) != set(range(11))
                or type(case["returned_slot"]) is not int
                or not 0 <= case["returned_slot"] < 5
            ):
                raise ValueError("Invalid quota plan")
        elif domain == "receipts":
            if (
                set(case) != {"domain", "kind", "boundary"}
                or case["kind"] not in ("loan", "reservation")
                or case["boundary"] not in ("before", "at", "after")
            ):
                raise ValueError("Invalid receipt plan")
        elif domain not in ("maintenance", "expired_receipt") or set(case) != {"domain"}:
            raise ValueError("Invalid circulation domain")
    return cases


def run_circulation_corpus(persistence, corpus):
    cases = validate_circulation_corpus(corpus)  # Validate all cases before any I/O.
    steps = 0
    for case in cases:
        if case["domain"] == "fifo":
            steps += fifo_scenario(
                case["order"], case["cancelled"], case["expiry"], backend=persistence
            )
        elif case["domain"] == "quotas":
            steps += quota_scenario(case["order"], case["returned_slot"], backend=persistence)
        elif case["domain"] == "receipts":
            steps += receipt_scenario(case["kind"], case["boundary"], backend=persistence)
        elif case["domain"] == "maintenance":
            steps += maintenance_scenario(backend=persistence)
        else:
            steps += expired_receipt_scenario(backend=persistence)
    return {
        "sequences_completed": len(cases),
        "service_actions_completed": steps,
        "generator_executed_here": False,
        "native_failure_shrinking": False,
        "concurrent_engine_exercised": False,
    }


def exercise_circulation_corpus(database, corpus):
    validate_circulation_corpus(corpus)
    from app.persistence.mongo import MongoPersistence

    return run_circulation_corpus(MongoPersistence(database), corpus)
