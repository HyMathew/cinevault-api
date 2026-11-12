"""Operation sequences against real files and an independent symbolic model."""

from hypothesis import given, settings
from hypothesis import strategies as st

from .generators import ACTION
from .harness import private_scenario, receipt_scenario
from .metrics import record


@settings(max_examples=25, deadline=None)
@given(st.lists(ACTION, min_size=16, max_size=16))
def test_private_sequences(actions):
    steps = private_scenario(actions)
    record("private_json", steps)


@settings(max_examples=25, deadline=None)
@given(st.lists(ACTION, min_size=12, max_size=12))
def test_receipt_sequences(actions):
    steps = receipt_scenario(actions)
    record("receipts_json", steps)
