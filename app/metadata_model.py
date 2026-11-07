"""Storage-safe source annotation; no networking or Flask imports."""

import re

QID = re.compile(r"Q[1-9][0-9]{0,11}")


def valid_source(value):
    return value is None or (
        isinstance(value, dict)
        and set(value) == {"qid", "revision", "retrieved_at"}
        and isinstance(value["qid"], str)
        and QID.fullmatch(value["qid"]) is not None
        and type(value["revision"]) is int
        and 1 <= value["revision"] <= 10**15
        and type(value["retrieved_at"]) is int
        and 0 <= value["retrieved_at"] <= 2**53
    )
