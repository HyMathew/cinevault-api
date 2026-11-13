"""Deterministic contention release; no database or wall-clock timing claim."""

import pytest
from test_mongo_transactions import Client, failure, implementation


class Clock:
    def __init__(self):
        self.now = 0.0
        self.pauses = []
        self.ceilings = []

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds

    def install(self, monkeypatch, *, fraction=1.0):
        module = implementation()
        monkeypatch.setattr(module, "monotonic", lambda: self.now)
        monkeypatch.setattr(module, "sleep", self.sleep)

        def uniform(low, high):
            assert low == 0
            self.ceilings.append(high)
            return high * fraction

        monkeypatch.setattr(module, "uniform", uniform, raising=False)
        return module


@pytest.mark.parametrize("release", [0.02, 0.12, 0.5, 2.0])
def test_default_retries_converge_after_contention_release(monkeypatch, release):
    clock = Clock()
    module = clock.install(monkeypatch)
    client = Client()
    calls = []
    identity = object()

    def command(_):
        calls.append(clock.now)
        if clock.now < release:
            raise failure("TransientTransactionError", code=112)
        return identity

    assert module.TransactionRunner(client).run(command) is identity
    assert calls[-1] >= release
    assert clock.now < 10
    assert client.trace.count("abort") == len(calls) - 1
    assert client.trace.count("commit") == 1


def test_explicit_three_attempts_still_exhaust_before_later_release(monkeypatch):
    clock = Clock()
    module = clock.install(monkeypatch, fraction=0)
    client = Client()
    calls = []

    def command(_):
        calls.append(clock.now)
        raise failure("TransientTransactionError", code=112)

    with pytest.raises(module.TransactionUnavailable):
        module.TransactionRunner(client, max_attempts=3).run(command)
    assert len(calls) == client.trace.count("abort") == 3
    assert "commit" not in client.trace


def test_zero_jitter_has_finite_default_attempt_cap(monkeypatch):
    clock = Clock()
    module = clock.install(monkeypatch, fraction=0)
    client = Client()
    calls = []

    def command(_):
        calls.append(clock.now)
        raise failure("TransientTransactionError", code=112)

    with pytest.raises(module.TransactionUnavailable):
        module.TransactionRunner(client).run(command)
    assert len(calls) == client.trace.count("abort") == 64
    assert len(clock.pauses) == 63
    assert clock.now == 0
    assert "commit" not in client.trace


@pytest.mark.parametrize("fraction", [0, 0.5, 1])
def test_full_jitter_uses_progressive_capped_windows(monkeypatch, fraction):
    clock = Clock()
    module = clock.install(monkeypatch, fraction=fraction)
    client = Client()
    calls = 0

    def command(_):
        nonlocal calls
        calls += 1
        if calls <= 7:
            raise failure("TransientTransactionError", code=112)
        return "released"

    assert module.TransactionRunner(client).run(command) == "released"
    assert clock.ceilings == pytest.approx([0.02, 0.04, 0.08, 0.16, 0.32, 0.5, 0.5])
    assert clock.pauses == pytest.approx(
        [
            0.02 * fraction,
            0.04 * fraction,
            0.08 * fraction,
            0.16 * fraction,
            0.32 * fraction,
            0.5 * fraction,
            0.5 * fraction,
        ]
    )
    assert client.trace.count("commit") == 1


def test_last_pause_is_bounded_by_deadline_no_late_body(monkeypatch):
    clock = Clock()
    module = clock.install(monkeypatch)
    client = Client()
    starts = []

    def command(_):
        starts.append(clock.now)
        raise failure("TransientTransactionError", code=112)

    with pytest.raises(module.TransactionUnavailable) as caught:
        module.TransactionRunner(client, timeout_seconds=0.1).run(command)
    assert not isinstance(caught.value, module.CommitOutcomeUnknown)
    assert starts == pytest.approx([0, 0.02, 0.06])
    assert clock.pauses == pytest.approx([0.02, 0.04, 0.04])
    assert clock.now == pytest.approx(0.1)
    assert "commit" not in client.trace


@pytest.mark.parametrize("code", [11000, 121, 112])
def test_nontransient_error_never_retries_or_sleeps(monkeypatch, code):
    from pymongo.errors import OperationFailure

    clock = Clock()
    module = clock.install(monkeypatch)
    client = Client()

    def command(_):
        raise OperationFailure("mongodb://canary-secret@private", code)

    with pytest.raises(module.TransactionUnavailable) as caught:
        module.TransactionRunner(client).run(command)
    assert client.trace == ["start", "abort", "end"]
    assert clock.pauses == []
    assert "canary" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("outcome", ["success", "exhaust", "unlabelled", "deadline"])
def test_unknown_commit_never_reexecutes_body(monkeypatch, outcome):
    from pymongo.errors import OperationFailure

    clock = Clock()
    module = clock.install(monkeypatch)
    errors = [failure("UnknownTransactionCommitResult")]
    if outcome in {"exhaust", "deadline"}:
        errors *= 8
    elif outcome == "unlabelled":
        errors.append(OperationFailure("mongodb://canary-secret@private", 121))
    client = Client(errors)
    tx = module.TransactionRunner(client, timeout_seconds=0.03 if outcome == "deadline" else 10)
    bodies = []

    def command(_):
        bodies.append(1)
        return "committed"

    if outcome == "success":
        assert tx.run(command) == "committed"
    else:
        with pytest.raises(module.CommitOutcomeUnknown) as caught:
            tx.run(command)
        assert "canary" not in str(caught.value)
    assert bodies == [1]
    assert client.trace.count("start") == 1
    assert (
        client.trace.count("commit")
        == {"success": 2, "exhaust": 5, "unlabelled": 2, "deadline": 2}[outcome]
    )
    assert clock.ceilings == []  # Body jitter never affects same-commit retries.
    if outcome == "deadline":
        assert clock.pauses == pytest.approx([0.02, 0.01])


def test_persistent_contention_uses_default_time_budget_without_late_body(monkeypatch):
    clock = Clock()
    module = clock.install(monkeypatch)
    client = Client()
    starts = []

    def command(_):
        starts.append(clock.now)
        raise failure("TransientTransactionError", code=112)

    with pytest.raises(module.TransactionUnavailable):
        module.TransactionRunner(client).run(command)
    assert clock.now == pytest.approx(10)
    assert 3 < len(starts) < 64
    assert all(start < 10 for start in starts)
    assert all(0 <= pause <= 0.5 for pause in clock.pauses)
    assert clock.pauses[-1] == pytest.approx(0.38)
    assert client.trace.count("abort") == len(starts)
    assert "commit" not in client.trace


def test_transient_commit_uses_body_backoff_before_new_transaction(monkeypatch):
    clock = Clock()
    module = clock.install(monkeypatch)
    client = Client([failure("TransientTransactionError"), None])
    bodies = []

    def command(_):
        bodies.append(clock.now)
        return "committed"

    assert module.TransactionRunner(client).run(command) == "committed"
    assert bodies == pytest.approx([0, 0.02])
    assert clock.ceilings == [0.02]
    assert client.trace == ["start", "commit", "start", "commit", "end"]


def test_unknown_followed_by_transient_commit_keeps_existing_driver_protocol(monkeypatch):
    clock = Clock()
    module = clock.install(monkeypatch)
    client = Client(
        [failure("UnknownTransactionCommitResult"), failure("TransientTransactionError"), None]
    )
    bodies = []

    def command(_):
        bodies.append(clock.now)
        return "committed"

    assert module.TransactionRunner(client).run(command) == "committed"
    assert bodies == pytest.approx([0, 0.04])
    assert clock.pauses == pytest.approx([0.02, 0.02])
    assert clock.ceilings == [0.02]
    assert client.trace == ["start", "commit", "commit", "start", "commit", "end"]
