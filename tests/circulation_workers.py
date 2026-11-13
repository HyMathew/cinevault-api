"""Owned process workers for real-file and real-Mongo circulation races."""

import multiprocessing
import re
from time import monotonic

_LABELS = ("TransientTransactionError", "UnknownTransactionCommitResult")
_STAGES = frozenset(
    {
        "initializing",
        "json_open",
        "mongo_client",
        "mongo_ping",
        "mongo_persistence",
        "service_setup",
        "barrier",
        "operation",
        "result",
    }
)


def _symbol(value, *, filename=False):
    if type(value) is not str:
        return "<other>"
    if filename:
        value = value.replace("\\", "/").rsplit("/", 1)[-1]
    if len(value) > 96 or re.search(r"[0-9a-fA-F]{16}", value.replace("-", "")):
        return "<other>"
    if value in {
        "<stdin>",
        "<string>",
        "<module>",
        "<lambda>",
        "<listcomp>",
        "<dictcomp>",
        "<setcomp>",
        "<genexpr>",
    }:
        return value
    pattern = r"[A-Za-z_][A-Za-z0-9_.-]*\.py" if filename else r"[A-Za-z_][A-Za-z0-9_]*"
    return value if re.fullmatch(pattern, value) else "<other>"


def _elapsed_ms(started):
    return None if started is None else min(60_000, max(0, int((monotonic() - started) * 1000)))


def failure_diagnostic(exc, stage, *, operation_started=None):
    """Bounded metadata only: never format exceptions, source lines or locals."""
    frames, cursor = [], exc.__traceback__
    for _ in range(64):
        if cursor is None:
            break
        code = cursor.tb_frame.f_code
        line = cursor.tb_lineno
        frames.append(
            {
                "file": _symbol(code.co_filename, filename=True),
                "function": _symbol(code.co_name),
                "line": line if type(line) is int and 0 <= line <= 10_000_000 else 0,
            }
        )
        cursor = cursor.tb_next
    chain, seen, current = [], set(), exc
    while isinstance(current, BaseException) and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        try:
            code = getattr(current, "code", None)
        except BaseException:
            code = None
        if type(code) is not int or not -(2**31) <= code < 2**31:
            code = None
        labels = []
        try:
            method = getattr(current, "has_error_label", None)
            if callable(method):
                labels = [label for label in _LABELS if method(label) is True]
        except BaseException:
            pass
        chain.append({"type": _symbol(type(current).__name__), "code": code, "labels": labels})
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return {
        "exception": _symbol(type(exc).__name__),
        "stage": stage if type(stage) is str and stage in _STAGES else "unknown_stage",
        "elapsed_ms": _elapsed_ms(operation_started),
        "frames": frames[-12:],
        "chain": chain,
    }


def _worker(configuration, operation, barrier, results):
    client = None
    stage = "initializing"
    operation_started = None
    try:
        if configuration["backend"] == "json":
            stage = "json_open"
            from app.persistence.json import JsonPersistence

            persistence = JsonPersistence(configuration["path"])
        else:
            stage = "mongo_client"
            from pymongo import MongoClient

            from app.persistence.mongo import MongoPersistence

            client = MongoClient(
                configuration["address"],
                username=configuration["username"],
                password=configuration["password"],
                authSource=configuration["database"],
                **configuration["options"],
            )
            stage = "mongo_ping"
            client.admin.command("ping")
            stage = "mongo_persistence"
            persistence = MongoPersistence(client[configuration["database"]])
        stage = "service_setup"
        from app.services.circulation import CirculationService

        service = CirculationService(persistence)
        stage = "barrier"
        barrier.wait(timeout=15)
        stage = "operation"
        operation_started = monotonic()
        result = getattr(service, operation["method"])(*operation["args"], **operation["kwargs"])
        elapsed_ms = _elapsed_ms(operation_started)
        stage = "result"
        results.put(
            {
                "status": result.status,
                "id": result.body.get("id") if result.body else None,
                "code": result.error.get("code") if result.error else None,
                "elapsed_ms": elapsed_ms,
            }
        )
    except BaseException as exc:
        results.put(failure_diagnostic(exc, stage, operation_started=operation_started))
    finally:
        if client is not None:
            client.close()


def race(configuration, operations):
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(len(operations)), context.Queue()
    processes = [
        context.Process(target=_worker, args=(configuration, op, barrier, results))
        for op in operations
    ]
    try:
        for process in processes:
            process.start()
        observed = [results.get(timeout=35) for _ in processes]
        for process in processes:
            process.join(5)
            assert process.exitcode == 0, "Circulation worker failed or did not exit"
        assert not any("exception" in row for row in observed), observed
        return observed
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()
        results.join_thread()
