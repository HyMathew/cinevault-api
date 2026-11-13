"""Persistence outcomes usable by HTTP without importing an optional driver."""


class TransactionUnavailable(RuntimeError):
    """No successful transaction result can be returned."""


class CommitOutcomeUnknown(TransactionUnavailable):
    """Commit may have succeeded; reconcile state instead of blindly replaying."""
