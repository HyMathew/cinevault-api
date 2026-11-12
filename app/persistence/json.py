"""Explicit JSON unit of work over the existing atomic, fully validated store."""

from collections.abc import Callable
from pathlib import Path

from ..storage_v2 import V2Store
from .contracts import ReadWork, T, Work
from .execution import ExecutionGuard
from .json_repositories import (
    JsonAccounts,
    JsonAttempts,
    JsonCatalogue,
    JsonEvents,
    JsonPersonal,
    JsonReceipts,
    JsonSessions,
    _DocumentScope,
)


class _JsonWork:
    def __init__(self, document, *, writable):
        self._scope = _DocumentScope(document, writable=writable)
        self._revision = document["revision"] + int(writable)
        self._accounts = JsonAccounts(self._scope)
        self._sessions = JsonSessions(self._scope)
        self._attempts = JsonAttempts(self._scope)
        self._catalogue = JsonCatalogue(self._scope)
        self._receipts = JsonReceipts(self._scope)
        self._personal = JsonPersonal(self._scope)
        self._events = JsonEvents(self._scope)

    @property
    def personal(self) -> JsonPersonal:
        self._scope.check()
        return self._personal

    @property
    def events(self) -> JsonEvents:
        self._scope.check()
        return self._events

    @property
    def revision(self) -> int:
        self._scope.check()
        return self._revision

    @property
    def accounts(self) -> JsonAccounts:
        self._scope.check()
        return self._accounts

    @property
    def sessions(self) -> JsonSessions:
        self._scope.check()
        return self._sessions

    @property
    def catalogue(self) -> JsonCatalogue:
        self._scope.check()
        return self._catalogue

    @property
    def attempts(self) -> JsonAttempts:
        self._scope.check()
        return self._attempts

    @property
    def receipts(self) -> JsonReceipts:
        self._scope.check()
        return self._receipts

    def _close(self):
        self._scope.close()


class JsonPersistence:
    def __init__(self, path: str | Path):
        self._store = V2Store(path)
        self._execution = ExecutionGuard()

    def run(self, command: Callable[[Work], T]) -> T:
        with self._execution.enter(command):

            def apply(document):
                work = _JsonWork(document, writable=True)
                try:
                    return self._execution.result(command(work))
                finally:
                    work._close()

            return self._store.transact(apply)

    def query(self, query: Callable[[ReadWork], T]) -> T:
        with self._execution.enter(query):
            work = _JsonWork(self._store.read(), writable=False)
            try:
                return self._execution.result(query(work))
            finally:
                work._close()

    def readiness(self) -> None:
        self.query(lambda work: None)
