"""Portable P4 export/replay and fail-closed validation before persistence I/O."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from .circulation_corpus import (
    exercise_circulation_corpus,
    run_circulation_corpus,
    validate_circulation_corpus,
)
from .harness import scenario_fixture
from .metrics import record


def test_p4_export_reproducible_and_replayed_on_real_json(tmp_path):
    script = Path(__file__).with_name("export_circulation_corpus.py")
    targets = [tmp_path / "first.json", tmp_path / "second.json"]
    for target in targets:
        subprocess.run(
            [sys.executable, str(script), "--output", str(target)], check=True, capture_output=True
        )
    assert targets[0].read_bytes() == targets[1].read_bytes()
    corpus = json.loads(targets[0].read_text(encoding="utf-8"))
    assert len(validate_circulation_corpus(corpus)) == 8
    assert [case["domain"] for case in corpus["cases"]] == [
        "fifo",
        "quotas",
        "receipts",
        "fifo",
        "quotas",
        "receipts",
        "maintenance",
        "expired_receipt",
    ]
    with scenario_fixture() as fixture:  # Intentionally prepopulated.
        result = run_circulation_corpus(fixture.backend, corpus)
        assert result["sequences_completed"] == 8
        assert 100 <= result["service_actions_completed"] <= 240
        assert result["generator_executed_here"] is False
        assert result["native_failure_shrinking"] is False
        assert result["concurrent_engine_exercised"] is False
        record("circulation_corpus_json", result["service_actions_completed"])


def valid_corpus():
    return {
        "format": 1,
        "domain": "circulation",
        "seed": 20261113,
        "cases": [
            {"domain": "fifo", "order": [0, 1, 3], "cancelled": [], "expiry": "at"},
            {"domain": "quotas", "order": list(range(11)), "returned_slot": 0},
            {"domain": "receipts", "kind": "loan", "boundary": "at"},
            {"domain": "maintenance"},
        ],
    }


@pytest.mark.parametrize(
    "defect",
    [
        "boolean-order",
        "duplicate-order",
        "foreign-cancel",
        "duplicate-cancel",
        "boolean-returned",
        "extra-field",
        "unknown-kind",
        "last-invalid",
        "boolean-format",
        "unbounded",
    ],
)
def test_entire_p4_corpus_validated_before_io(defect):
    class NoIO:
        def run(self, _):
            raise AssertionError("Invalid corpus reached persistence")

        query = run

    corpus = copy.deepcopy(valid_corpus())
    if defect == "boolean-order":
        corpus["cases"][0]["order"][0] = False
    elif defect == "duplicate-order":
        corpus["cases"][0]["order"] = [0, 0, 3]
    elif defect == "foreign-cancel":
        corpus["cases"][0]["cancelled"] = [2]
    elif defect == "duplicate-cancel":
        corpus["cases"][0]["cancelled"] = [0, 0]
    elif defect == "boolean-returned":
        corpus["cases"][1]["returned_slot"] = True
    elif defect == "extra-field":
        corpus["cases"][1]["unexpected"] = 0
    elif defect == "unknown-kind":
        corpus["cases"][2]["kind"] = "admin"
    elif defect == "last-invalid":
        corpus["cases"][-1] = {"domain": "unknown"}
    elif defect == "boolean-format":
        corpus["format"] = True
    else:
        corpus["cases"] *= 3
    with pytest.raises(ValueError):
        run_circulation_corpus(NoIO(), corpus)
    with pytest.raises(ValueError):
        exercise_circulation_corpus(None, corpus)  # Reject before database.client is inspected.


def test_controller_never_overwrites_existing_corpus(tmp_path):
    target = tmp_path / "preserved.json"
    target.write_bytes(b"preserved")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("export_circulation_corpus.py")),
            "--output",
            str(target),
        ],
        capture_output=True,
    )
    assert result.returncode == 2 and target.read_bytes() == b"preserved"
