"""Synchronous, eager and non-nested execution shared by both adapters."""

import inspect
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Future
from contextlib import contextmanager


class UnitOfWorkProtocolError(RuntimeError):
    """A callback cannot be executed safely inside this unit of work."""


class ExecutionGuard:
    def __init__(self):
        self._local = threading.local()

    @contextmanager
    def enter(self, callback):
        if getattr(self._local, "active", False):
            raise UnitOfWorkProtocolError("A nested unit of work on this instance is not allowed")
        if not callable(callback):
            raise UnitOfWorkProtocolError("Unit of work requires a synchronous callback")
        if any(
            check(target)
            for target in (callback, getattr(callback, "__call__", None))
            for check in (
                inspect.iscoroutinefunction,
                inspect.isgeneratorfunction,
                inspect.isasyncgenfunction,
            )
        ):
            raise UnitOfWorkProtocolError("Unit of work callback must be synchronous and eager")
        self._local.active = True
        try:
            yield
        finally:
            self._local.active = False

    @staticmethod
    def result(value):
        if inspect.isawaitable(value) or isinstance(value, (Iterator, AsyncIterator, Future)):
            if inspect.iscoroutine(value) or inspect.isgenerator(value):
                value.close()
            raise UnitOfWorkProtocolError("Unit of work result must be eager")
        return value
