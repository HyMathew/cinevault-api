"""Bound anonymous registration work across processes, outside the business lock."""

import json
import re
from pathlib import Path

import portalocker

from .repository import unique_pairs
from .storage_v2 import _encode, _replace

WINDOW = 60
SOURCE_LIMIT = 20
GLOBAL_LIMIT = 60


class RegistrationBusy(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, min(WINDOW, retry_after))


class RegistrationAdmission:
    def __init__(self, store_path):
        self.path = Path(str(store_path) + ".registration.json")
        self.lock_path = str(self.path) + ".lock"

    def read(self, timestamp):
        empty = {"version": 1, "window_start": timestamp, "total": 0, "sources": {}}
        if not self.path.exists():
            return empty
        with self.path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Invalid registration admission state")
        state = json.loads(raw, object_pairs_hook=unique_pairs)
        if (
            not isinstance(state, dict)
            or set(state) != {"version", "window_start", "total", "sources"}
            or type(state["version"]) is not int
            or state["version"] != 1
            or type(state["window_start"]) is not int
            or not 0 <= state["window_start"] <= timestamp
            or type(state["total"]) is not int
            or not 0 <= state["total"] <= GLOBAL_LIMIT
            or not isinstance(state["sources"], dict)
            or len(state["sources"]) > GLOBAL_LIMIT
        ):
            raise ValueError("Invalid registration admission state")
        if (
            any(
                not re.fullmatch(r"[0-9a-f]{64}", key)
                or type(count) is not int
                or not 1 <= count <= SOURCE_LIMIT
                for key, count in state["sources"].items()
            )
            or sum(state["sources"].values()) != state["total"]
        ):
            raise ValueError("Invalid registration admission counters")
        return empty if state["window_start"] + WINDOW <= timestamp else state

    def compute(self, source_key, timestamp, operation):
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Invalid admission clock")
        lock = portalocker.Lock(
            self.lock_path, mode="a", timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB
        )
        try:
            lock.acquire()
        except portalocker.exceptions.AlreadyLocked:
            raise RegistrationBusy(1) from None
        try:
            state = self.read(timestamp)
            if (
                state["total"] >= GLOBAL_LIMIT
                or state["sources"].get(source_key, 0) >= SOURCE_LIMIT
            ):
                raise RegistrationBusy(state["window_start"] + WINDOW - timestamp)
            state["total"] += 1
            state["sources"][source_key] = state["sources"].get(source_key, 0) + 1
            _replace(self.path, _encode(state))
            # Reserve before work, including on worker failure. Only this registration
            # lock is held; normal catalogue/session transactions use their own lock.
            return operation()
        finally:
            lock.release()
