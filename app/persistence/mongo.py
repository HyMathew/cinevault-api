"""Targeted MongoDB UoW; provisioning and HTTP activation are separate concerns."""

from collections.abc import Callable
from os import getpid

from bson import Int64
from pymongo import ReturnDocument

from .contracts import ReadWork, T, Work
from .execution import ExecutionGuard
from .mongo_model import INT64_MAX
from .mongo_repositories import (
    MongoAccounts,
    MongoAttempts,
    MongoCatalogue,
    MongoSessions,
    _MongoScope,
)
from .mongo_transactions import TransactionRunner


class _MongoWork:
    def __init__(self, scope):
        self._scope = scope
        self._accounts = MongoAccounts(scope)
        self._sessions = MongoSessions(scope)
        self._attempts = MongoAttempts(scope)
        self._catalogue = MongoCatalogue(scope)

    @property
    def revision(self) -> int:
        self._scope.check()
        return self._scope._revision

    @property
    def accounts(self) -> MongoAccounts:
        self._scope.check()
        return self._accounts

    @property
    def sessions(self) -> MongoSessions:
        self._scope.check()
        return self._sessions

    @property
    def catalogue(self) -> MongoCatalogue:
        self._scope.check()
        return self._catalogue

    @property
    def attempts(self) -> MongoAttempts:
        self._scope.check()
        return self._attempts


class MongoPersistence:
    def __init__(self, database):
        self._database = database
        self._owner_pid = getpid()
        self._runner = TransactionRunner(database.client)
        self._execution = ExecutionGuard()

    def _execute(self, callback, *, writable):
        if self._owner_pid != getpid():
            raise RuntimeError("MongoDB persistence must stay in its owning process")
        with self._execution.enter(callback):

            def apply(session):
                if writable:
                    coordinate = self._database.coordination.find_one_and_update(
                        {"_id": "business", "revision": {"$lt": Int64(INT64_MAX)}},
                        {"$inc": {"revision": Int64(1)}},
                        return_document=ReturnDocument.AFTER,
                        session=session,
                    )
                else:
                    coordinate = self._database.coordination.find_one(
                        {"_id": "business"}, session=session
                    )
                if (
                    coordinate is None
                    or set(coordinate) != {"_id", "revision"}
                    or type(coordinate["revision"]) is not Int64
                    or not 0 <= coordinate["revision"] <= INT64_MAX
                ):
                    raise ValueError("MongoDB coordination is absent, invalid or exhausted")
                scope = _MongoScope(
                    self._database, session, int(coordinate["revision"]), writable=writable
                )
                try:
                    result = self._execution.result(callback(_MongoWork(scope)))
                    scope.validate()
                    return result
                finally:
                    scope.close()

            return self._runner.run(apply)

    def run(self, command: Callable[[Work], T]) -> T:
        return self._execute(command, writable=True)

    def query(self, query: Callable[[ReadWork], T]) -> T:
        return self._execute(query, writable=False)

    def readiness(self) -> None:
        self.query(lambda work: None)
