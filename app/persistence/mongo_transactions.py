"""Bounded transaction/commit retries with explicit uncertain outcomes.

Commands must use the supplied session, propagate driver errors and have no
external side effects. This runner does not supply business authorization.
"""

import math
from time import monotonic, sleep

import pymongo
from pymongo import ReadPreference
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from .execution import ExecutionGuard


class TransactionUnavailable(RuntimeError):
    """No successful transaction result can be returned."""


class CommitOutcomeUnknown(TransactionUnavailable):
    """Commit may have succeeded; resolve via state/idempotency, never blind replay."""


class TransactionProtocolError(RuntimeError):
    """A command tried to manage the transaction owned by the runner."""


def _abort(session):
    if session.in_transaction:
        try:
            session.abort_transaction()
        except PyMongoError:
            # No commit was requested here. Preserve the command's original
            # exception; server transaction expiration also prevents commit.
            pass


class TransactionRunner:
    def __init__(self, client, *, timeout_seconds=10, max_attempts=3, max_commit_attempts=5):
        if (
            type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 60
            or not math.isfinite(timeout_seconds)
            or type(max_attempts) is not int
            or max_attempts < 1
            or type(max_commit_attempts) is not int
            or max_commit_attempts < 1
        ):
            raise ValueError("Invalid transaction retry configuration")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_commit_attempts = max_commit_attempts
        self._execution = ExecutionGuard()

    @staticmethod
    def _pause(deadline, *, uncertain=False):
        remaining = deadline - monotonic()
        if remaining <= 0:
            if uncertain:
                raise CommitOutcomeUnknown("MongoDB commit outcome is unknown") from None
            raise TransactionUnavailable("MongoDB transaction deadline exceeded") from None
        sleep(min(0.02, remaining))

    def _commit(self, session, deadline):
        """True means committed; False permits a new complete transaction."""
        uncertain = False
        for attempt in range(self.max_commit_attempts):
            if monotonic() >= deadline:
                if uncertain:
                    raise CommitOutcomeUnknown("MongoDB commit outcome is unknown") from None
                _abort(session)
                raise TransactionUnavailable("MongoDB transaction deadline exceeded") from None
            try:
                session.commit_transaction()
                return True
            except PyMongoError as exc:
                if exc.has_error_label("UnknownTransactionCommitResult"):
                    uncertain = True
                    if getattr(exc, "code", None) == 50 or attempt + 1 == self.max_commit_attempts:
                        raise CommitOutcomeUnknown("MongoDB commit outcome is unknown") from None
                    self._pause(deadline, uncertain=True)
                    continue
                if exc.has_error_label("TransientTransactionError"):
                    return False
                if uncertain:
                    raise CommitOutcomeUnknown("MongoDB commit outcome is unknown") from None
                raise TransactionUnavailable("MongoDB transaction unavailable") from None
        raise AssertionError("Commit retry loop exhausted without an outcome")

    def run(self, command):
        with self._execution.enter(command):
            return self._run(command)

    def _run(self, command):
        deadline = monotonic() + self.timeout_seconds
        try:
            # CSOT bounds driver calls, while monotonic checks bound our retries.
            # Python CPU work inside a command is not preempted by this timeout.
            with pymongo.timeout(self.timeout_seconds):
                with self.client.start_session() as session:
                    for attempt in range(self.max_attempts):
                        if monotonic() >= deadline:
                            raise TransactionUnavailable("MongoDB transaction deadline exceeded")
                        session.start_transaction(
                            read_concern=ReadConcern("snapshot"),
                            write_concern=WriteConcern("majority", wtimeout=5000),
                            read_preference=ReadPreference.PRIMARY,
                        )
                        try:
                            result = self._execution.result(command(session))
                        except BaseException as exc:
                            _abort(session)
                            if not isinstance(exc, PyMongoError):
                                raise
                            if exc.has_error_label("TransientTransactionError"):
                                if attempt + 1 < self.max_attempts:
                                    self._pause(deadline)
                                    continue
                            raise TransactionUnavailable(
                                "MongoDB transaction unavailable"
                            ) from None
                        if not session.in_transaction:
                            raise TransactionProtocolError("Command ended its owned transaction")
                        if self._commit(session, deadline):
                            return result
                        if attempt + 1 < self.max_attempts:
                            self._pause(deadline)
                    raise TransactionUnavailable("MongoDB transaction retries exhausted") from None
        except PyMongoError:
            # URI, host details, command bodies and driver errors do not cross
            # this boundary. The HTTP layer can report a stable unavailable code.
            raise TransactionUnavailable("MongoDB transaction unavailable") from None
