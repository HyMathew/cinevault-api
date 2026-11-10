"""Deterministic fault injection for retry decisions, not a simulated DB gate."""

import importlib.util
from concurrent.futures import Future

import pytest
from pymongo.errors import OperationFailure


def implementation():
    assert importlib.util.find_spec("app.persistence.mongo_transactions") is not None, (
        "The bounded MongoDB transaction runner is not implemented"
    )
    from app.persistence import mongo_transactions

    return mongo_transactions


def failure(label, code=251):
    return OperationFailure(
        "synthetic driver detail must not escape", code, {"errorLabels": [label]}
    )


class Session:
    def __init__(self, trace, commit_errors=()):
        self.trace = trace
        self.commit_errors = iter(commit_errors)
        self.in_transaction = False
        self.transaction_options = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.in_transaction:
            self.abort_transaction()
        self.trace.append("end")

    def start_transaction(self, **kwargs):
        self.trace.append("start")
        self.transaction_options.append(kwargs)
        self.in_transaction = True

    def commit_transaction(self):
        self.trace.append("commit")
        # The real pinned driver sets COMMITTED even when commit raises.
        self.in_transaction = False
        problem = next(self.commit_errors, None)
        if problem:
            raise problem

    def abort_transaction(self):
        self.trace.append("abort")
        self.in_transaction = False


class Client:
    def __init__(self, commit_errors=()):
        self.trace = []
        self.session = Session(self.trace, commit_errors)
        self.started = 0

    def start_session(self):
        self.started += 1
        return self.session


def runner(client, monkeypatch, **kwargs):
    module = implementation()
    monkeypatch.setattr(module, "sleep", lambda _: None)
    return module.TransactionRunner(client, **kwargs)


def test_result_is_returned_only_after_commit_and_session_closes(monkeypatch):
    client = Client()
    tx = runner(client, monkeypatch)

    def command(session):
        assert session is client.session
        client.trace.append("body")
        return {"id": "synthetic"}

    assert tx.run(command) == {"id": "synthetic"}
    assert client.trace == ["start", "body", "commit", "end"]
    options = client.session.transaction_options[0]
    assert options["read_concern"].document == {"level": "snapshot"}
    assert options["write_concern"].document["w"] == "majority"
    assert options["read_preference"].name == "Primary"


def test_domain_exception_aborts_and_is_preserved_without_replay(monkeypatch):
    client = Client()
    tx = runner(client, monkeypatch)
    error = ValueError("business refusal")

    def command(_):
        client.trace.append("body")
        raise error

    with pytest.raises(ValueError) as caught:
        tx.run(command)
    assert caught.value is error
    assert client.trace == ["start", "body", "abort", "end"]


def test_transient_body_error_aborts_then_reexecutes_whole_command(monkeypatch):
    client = Client()
    tx = runner(client, monkeypatch)
    calls = 0

    def command(_):
        nonlocal calls
        calls += 1
        client.trace.append("body")
        if calls == 1:
            raise failure("TransientTransactionError")
        return "done"

    assert tx.run(command) == "done"
    assert client.trace == ["start", "body", "abort", "start", "body", "commit", "end"]


def test_unknown_commit_retries_same_commit_without_reexecuting_command(monkeypatch):
    client = Client([failure("UnknownTransactionCommitResult"), None])
    tx = runner(client, monkeypatch)

    def command(_):
        client.trace.append("body")
        return "done"

    assert tx.run(command) == "done"
    assert client.trace == ["start", "body", "commit", "commit", "end"]
    assert client.started == 1


def test_transient_commit_error_reexecutes_command_in_a_new_transaction(monkeypatch):
    client = Client([failure("TransientTransactionError"), None])
    tx = runner(client, monkeypatch)

    def command(_):
        client.trace.append("body")
        return "done"

    assert tx.run(command) == "done"
    assert client.trace == ["start", "body", "commit", "start", "body", "commit", "end"]


def test_exhausted_unknown_commit_reports_uncertainty_never_success(monkeypatch):
    module = implementation()
    client = Client([failure("UnknownTransactionCommitResult")] * 8)
    tx = runner(client, monkeypatch, max_commit_attempts=3)
    with pytest.raises(module.CommitOutcomeUnknown) as caught:
        tx.run(lambda _: client.trace.append("body"))
    assert client.trace == ["start", "body", "commit", "commit", "commit", "end"]
    assert "synthetic driver detail" not in str(caught.value)


def test_exhausted_transaction_retries_are_bounded(monkeypatch):
    module = implementation()
    client = Client()
    tx = runner(client, monkeypatch, max_attempts=3)

    def command(_):
        client.trace.append("body")
        raise failure("TransientTransactionError")

    with pytest.raises(module.TransactionUnavailable):
        tx.run(command)
    assert client.trace.count("body") == client.trace.count("abort") == 3
    assert "commit" not in client.trace


def test_deadline_expired_before_commit_aborts_without_an_uncertain_outcome(monkeypatch):
    module = implementation()
    client = Client()
    tx = runner(client, monkeypatch, timeout_seconds=1)
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    def command(_):
        client.trace.append("body")
        clock[0] = 2.0

    with pytest.raises(module.TransactionUnavailable) as caught:
        tx.run(command)
    assert not isinstance(caught.value, module.CommitOutcomeUnknown)
    assert client.trace == ["start", "body", "abort", "end"]


def test_max_time_commit_error_is_uncertain_and_does_not_retry_body(monkeypatch):
    module = implementation()
    client = Client([failure("UnknownTransactionCommitResult", code=50)])
    tx = runner(client, monkeypatch)
    with pytest.raises(module.CommitOutcomeUnknown):
        tx.run(lambda _: client.trace.append("body"))
    assert client.trace == ["start", "body", "commit", "end"]


def test_unlabelled_database_error_is_sanitized_and_not_replayed(monkeypatch):
    module = implementation()
    client = Client()
    tx = runner(client, monkeypatch)

    def command(_):
        raise OperationFailure("mongodb://secret@internal-detail", 121)

    with pytest.raises(module.TransactionUnavailable) as caught:
        tx.run(command)
    assert "secret" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert client.trace == ["start", "abort", "end"]


def test_callback_cannot_abort_then_return_an_apparent_success(monkeypatch):
    module = implementation()
    client = Client()
    tx = runner(client, monkeypatch)
    with pytest.raises(module.TransactionProtocolError):
        tx.run(lambda session: session.abort_transaction())
    assert client.trace == ["start", "abort", "end"]


def test_nested_runner_cannot_commit_independently_of_outer_command(monkeypatch):
    client = Client()
    tx = runner(client, monkeypatch)

    def command(_):
        client.trace.append("body")
        tx.run(lambda session: client.trace.append("nested-body"))

    with pytest.raises(RuntimeError, match="nested"):
        tx.run(command)
    assert client.started == 1
    assert client.trace == ["start", "body", "abort", "end"]
    assert tx.run(lambda session: "next") == "next"


def test_async_command_is_rejected_before_a_session_opens(monkeypatch):
    client = Client()
    tx = runner(client, monkeypatch)

    async def command(_):
        pytest.fail("A deferred command must never be scheduled")

    with pytest.raises(RuntimeError, match="synchronous"):
        tx.run(command)
    assert client.started == 0


def test_awaitable_result_is_rejected_and_aborted_before_commit(monkeypatch):
    client = Client()
    tx = runner(client, monkeypatch)

    async def value():
        return "not-executed"

    with pytest.raises(RuntimeError, match="eager"):
        tx.run(lambda session: value())
    assert client.trace == ["start", "abort", "end"]


def test_concurrent_future_is_not_a_successful_eager_result(monkeypatch):
    client = Client()
    tx = runner(client, monkeypatch)
    pending = Future()
    with pytest.raises(RuntimeError, match="eager"):
        tx.run(lambda session: pending)
    assert client.trace == ["start", "abort", "end"]
    assert not pending.done()


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"max_attempts": 0},
        {"max_attempts": True},
        {"max_commit_attempts": -1},
    ],
)
def test_invalid_retry_configuration_fails_before_opening_session(options):
    module = implementation()
    client = Client()
    with pytest.raises(ValueError):
        module.TransactionRunner(client, **options)
    assert client.started == 0
